"""Vertex AI Gemini client.

Auth resolution order (first that works wins):
  1. GOOGLE_APPLICATION_CREDENTIALS / Workload Identity Federation (CI)
  2. Local Application Default Credentials (`gcloud auth application-default login`)

Verified working: gemini-2.5-flash / gemini-2.5-flash-lite / gemini-2.5-pro
in us-central1.
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

PROJECT = os.environ.get("GCP_PROJECT", "zken-genai")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
ENDPOINT = (
    "https://{loc}-aiplatform.googleapis.com/v1/projects/{proj}"
    "/locations/{loc}/publishers/google/models/{model}:generateContent"
)

_creds_lock = threading.Lock()
_creds = None


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
    model: str = "gemini-2.5-flash",
    system: str | None = None,
    schema: dict[str, Any] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    retries: int = 4,
    thinking_budget: int | None = None,
) -> str | Any:
    """Call Gemini. If `schema` is given, returns parsed JSON; else raw text.

    Retries on 429/5xx with exponential backoff + jitter.

    `thinking_budget=0` disables Gemini 2.5 thinking. This matters a lot for
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

    url = ENDPOINT.format(loc=LOCATION, proj=PROJECT, model=model)
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
                return _extract(r.json(), schema)
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
        out = generate("Reply with exactly: OK", model="gemini-2.5-flash-lite", max_tokens=16)
        return "OK" in str(out).upper()
    except Exception as e:  # noqa: BLE001 - surfaced to the operator
        log.error("Vertex healthcheck failed: %s", e)
        return False
