"""Vertex AI Gemini client.

Auth resolution order (first that works wins):
  1. GOOGLE_APPLICATION_CREDENTIALS / Workload Identity Federation (CI)
  2. Local Application Default Credentials (`gcloud auth application-default login`)

Location defaults to `global` (override with GCP_LOCATION).
Verified working in global: gemini-3.6-flash, gemini-2.5-flash,
gemini-2.5-flash-lite, gemini-2.5-pro.
NOTE: gemini-3.6-pro and gemini-3.6-flash-lite do not exist (404 on Vertex).
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any

import google.auth
import google.auth.transport.requests
import requests

log = logging.getLogger(__name__)

PROJECT = os.environ.get("GCP_PROJECT") or ""
LOCATION = os.environ.get("GCP_LOCATION", "global")


def _endpoint(model: str) -> str:
    """Build the generateContent URL for the configured location.

    The `global` location does NOT use a region-prefixed host: it is
    `aiplatform.googleapis.com`, whereas regional is
    `us-central1-aiplatform.googleapis.com`. Getting this wrong 404s.
    """
    if not PROJECT:
        raise RuntimeError("set GCP_PROJECT (the Vertex AI project) in the environment")
    host = (
        "aiplatform.googleapis.com" if LOCATION == "global"
        else f"{LOCATION}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{PROJECT}/locations/{LOCATION}"
        f"/publishers/google/models/{model}:generateContent"
    )

_creds_lock = threading.Lock()
_creds = None

# Per-run token accounting, so a large backfill can report what it actually cost.
USAGE: dict[str, dict[str, int]] = {}
_usage_lock = threading.Lock()


def _record(model: str, meta: dict[str, Any]) -> None:
    with _usage_lock:
        row = USAGE.setdefault(model, {"calls": 0, "in": 0, "out": 0})
        row["calls"] += 1
        row["in"] += int(meta.get("promptTokenCount", 0) or 0)
        row["out"] += int(
            (meta.get("candidatesTokenCount", 0) or 0)
            + (meta.get("thoughtsTokenCount", 0) or 0)
        )


def usage_report() -> str:
    if not USAGE:
        return "no LLM calls"
    lines = [f"{'model':<26}{'calls':>7}{'in_tok':>12}{'out_tok':>10}"]
    tc = ti = to = 0
    for m, r in sorted(USAGE.items()):
        lines.append(f"{m:<26}{r['calls']:>7}{r['in']:>12,}{r['out']:>10,}")
        tc, ti, to = tc + r["calls"], ti + r["in"], to + r["out"]
    lines.append(f"{'TOTAL':<26}{tc:>7}{ti:>12,}{to:>10,}")
    return "\n".join(lines)


def _token() -> str:
    """Fetch (and lazily refresh) an access token. Thread-safe."""
    global _creds
    with _creds_lock:
        if _creds is None:
            _creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token


class LLMError(RuntimeError):
    pass


def generate(
    prompt: str,
    *,
    model: str = "gemini-3.6-flash",
    system: str | None = None,
    schema: dict[str, Any] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    retries: int = 4,
    thinking_budget: int | None = None,
) -> str | Any:
    """Call Gemini. If `schema` is given, returns parsed JSON; else raw text.

    Retries on 429/5xx with exponential backoff + jitter.

    `thinking_budget=0` disables thinking (supported on 2.5 and 3.6). This matters a lot for
    structured output: thinking tokens are billed against maxOutputTokens, so
    a reasoning-heavy call can exhaust the budget and return JSON truncated
    mid-string. Triage and summarisation don't need thinking; the digest does.
    """
    gen_cfg: dict[str, Any] = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    if thinking_budget is not None:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema

    url = _endpoint(model)
    last = ""
    for attempt in range(retries):
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=180,
            )
        except requests.RequestException as e:
            last = f"transport: {e}"
        else:
            if r.status_code == 200:
                payload = r.json()
                _record(model, payload.get("usageMetadata", {}) or {})
                return _extract(payload, schema)
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            # 4xx other than rate-limit is not retryable
            if r.status_code not in (408, 429) and r.status_code < 500:
                raise LLMError(last)

        sleep = min(60, 2**attempt) + random.uniform(0, 1.5)
        log.warning("Gemini retry %d/%d in %.1fs (%s)", attempt + 1, retries, sleep, last)
        time.sleep(sleep)

    raise LLMError(f"exhausted retries: {last}")


def _extract(payload: dict[str, Any], schema: dict[str, Any] | None) -> Any:
    cands = payload.get("candidates") or []
    if not cands:
        raise LLMError(f"no candidates (likely safety block): {str(payload)[:300]}")
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = cands[0].get("finishReason", "?")
        raise LLMError(f"empty response (finishReason={reason})")
    if schema is None:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        hint = ""
        if cands[0].get("finishReason") == "MAX_TOKENS":
            hint = " (hit maxOutputTokens — raise it or set thinking_budget=0)"
        raise LLMError(f"bad JSON{hint}: {e}: {text[:200]}") from e


def healthcheck() -> bool:
    """Cheap connectivity probe used by `run.py --check`."""
    try:
        # Checks the model the pipeline actually uses, so --check catches a
        # bad model id as well as bad credentials.
        # thinking_budget=0 is REQUIRED here: 3.6-flash thinks by default and
        # would spend the whole 16-token budget on it, returning MAX_TOKENS
        # with an empty body — a false failure.
        out = generate("Reply with exactly: OK", model="gemini-3.6-flash",
                       max_tokens=16, thinking_budget=0)
        return "OK" in str(out).upper()
    except Exception as e:  # noqa: BLE001 - surfaced to the operator
        log.error("Vertex healthcheck failed: %s", e)
        return False
