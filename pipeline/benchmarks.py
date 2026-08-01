"""Benchmark Arena — leaderboard *state*, not a news stream.

Each source publishes structured data (JSON/YAML) that its own site renders
client-side, so we read the same file the page reads rather than scraping DOM.
That is far more stable than parsing rendered HTML.

Adding a board = add a parser here + an entry under `benchmarks:` in
sources.yaml. Parsers must never raise: a dead board degrades to "unavailable"
and the rest of the page still renders.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

import requests
import yaml

log = logging.getLogger(__name__)

UA = {"User-Agent": "ai-wiki/1.0 (+https://github.com/zken-cloud/ai-wiki)"}
TIMEOUT = 45


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _num(v: Any, suffix: str = "") -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:g}{suffix}"
    return str(v)


def _sortkey(v: Any) -> float:
    """Numeric sort key tolerant of '88%', '—', and plain strings."""
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return float("-inf")


def _date(v: Any) -> str:
    """YAML parses ISO dates into date objects; str() would give a repr."""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v) if v else "—"


def _staleness(newest: str, threshold: int = 120) -> str:
    """Flag boards whose newest entry is old, so a stale table is never
    presented as the current state of the art."""
    try:
        age = (dt.date.today() - dt.date.fromisoformat(newest[:10])).days
    except (ValueError, TypeError):
        return ""
    return f"STALE: newest entry is {age} days old" if age > threshold else ""


# --------------------------------------------------------------------------- #
# Parsers — each returns {columns, rows, note}
# --------------------------------------------------------------------------- #

def cybergym(cfg: dict) -> dict:
    """cybergym.io — vulnerability-discovery benchmark, scored by difficulty level."""
    data = _get(cfg["url"]).json()
    level = cfg.get("level", "level1")
    rows = []
    for r in data.get(level, []):
        rows.append([
            r.get("agent", "—"),
            r.get("model", "—"),
            _num(r.get("score_10")),
            r.get("date") or r.get("model_release_date") or "—",
        ])
    rows.sort(key=lambda x: _sortkey(x[2]), reverse=True)
    return {
        "columns": ["Agent", "Model", "Score@10", "Date"],
        "rows": rows[: cfg.get("top", 12)],
        "note": f"{len(data.get(level, []))} entries on {level}",
    }


def cybergym_e2e(cfg: dict) -> dict:
    """cybergym.io end-to-end track: patch + exploit success."""
    data = _get(cfg["url"]).json()
    rows = [
        [
            r.get("model", "—"), r.get("harness", "—"),
            _num(r.get("patch_only"), "%"), _num(r.get("s1"), "%"), _num(r.get("s2"), "%"),
            r.get("budget", "—"),
        ]
        for r in data.get("results", [])
    ]
    rows.sort(key=lambda x: _sortkey(x[2]), reverse=True)
    return {
        "columns": ["Model", "Harness", "Patch only", "S1", "S2", "Budget"],
        "rows": rows[: cfg.get("top", 12)],
        "note": f"{len(data.get('results', []))} entries",
    }


def exploitgym(cfg: dict) -> dict:
    """cybergym.io ExploitGym — exploit-development capability."""
    data = _get(cfg["url"]).json()
    results = data.get("results", [])
    rows = [
        [
            r.get("model", "—"), r.get("agent", "—"),
            _num(r.get("userspace")), r.get("date", "—"), r.get("eval_note", "—"),
        ]
        for r in results
    ]
    rows.sort(key=lambda x: _sortkey(x[2]), reverse=True)
    return {
        "columns": ["Model", "Agent", "Userspace", "Date", "Eval"],
        "rows": rows[: cfg.get("top", 12)],
        "note": f"{len(results)} entries",
    }


def aider_polyglot(cfg: dict) -> dict:
    """Aider polyglot coding benchmark (read from the repo's source YAML)."""
    entries = yaml.safe_load(_get(cfg["url"]).text) or []
    entries = [e for e in entries if isinstance(e, dict) and e.get("pass_rate_2") is not None]
    entries.sort(key=lambda e: e.get("pass_rate_2", 0), reverse=True)
    rows = [
        [
            e.get("model", "—"),
            _num(e.get("pass_rate_2"), "%"),
            _num(e.get("percent_cases_well_formed"), "%"),
            _date(e.get("date")),
        ]
        for e in entries[: cfg.get("top", 12)]
    ]
    newest = max((_date(e.get("date")) for e in entries), default="")
    return {
        "columns": ["Model", "Pass rate", "Well-formed edits", "Date"],
        "rows": rows,
        "note": f"{len(entries)} entries · newest {newest or 'unknown'}",
        "stale": _staleness(newest),
    }


PARSERS: dict[str, Callable[[dict], dict]] = {
    "cybergym": cybergym,
    "cybergym_e2e": cybergym_e2e,
    "exploitgym": exploitgym,
    "aider_polyglot": aider_polyglot,
}


def fetch_all(boards: list[dict]) -> list[dict]:
    """Fetch every configured board. A failure degrades that board only."""
    out: list[dict] = []
    for b in boards:
        parser = PARSERS.get(b.get("parser", ""))
        entry = {
            "id": b.get("id", ""), "title": b.get("title", ""),
            "group": b.get("group", "Other"), "blurb": b.get("blurb", ""),
            "source_url": b.get("source_url") or b.get("url", ""),
            "columns": [], "rows": [], "note": "", "error": "", "stale": "",
        }
        if not parser:
            entry["error"] = f"no parser named {b.get('parser')!r}"
            log.error("benchmark %s: %s", entry["id"], entry["error"])
            out.append(entry)
            continue
        try:
            entry.update(parser(b))
            log.info("benchmark %s: %d rows", entry["id"], len(entry["rows"]))
        except Exception as e:  # noqa: BLE001 - one dead board must not kill the page
            entry["error"] = f"{type(e).__name__}: {e}"[:160]
            log.error("benchmark %s failed: %s", entry["id"], entry["error"])
        out.append(entry)
    return out
