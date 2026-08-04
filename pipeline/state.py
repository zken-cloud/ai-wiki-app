"""Dedupe ledger.

Lives in the *wiki* repo (`data/seen.json`) so it is committed alongside the
content it describes: GitHub Actions runners are stateless, and keeping the
ledger next to the output means the history is auditable in one place.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 120


class Seen:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                log.error("corrupt %s (%s); starting a fresh ledger", path, e)

    def is_new(self, uid: str) -> bool:
        return uid not in self.data

    def filter_new(self, items: list[dict]) -> list[dict]:
        fresh = [it for it in items if self.is_new(it["uid"])]
        log.info("dedupe: %d -> %d new", len(items), len(fresh))
        return fresh

    def mark(self, items: list[dict], date: str) -> None:
        for it in items:
            # Re-hydrated rows can carry a blank uid (older exports did not
            # store one). Marking those wrote a single "" key, after which
            # is_new("") is False for every future blank-uid item — they would
            # all be silently dropped as "already seen".
            uid = it.get("uid")
            if uid:
                self.data.setdefault(uid, date)

    def save(self) -> None:
        cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).isoformat()
        pruned = {k: v for k, v in self.data.items() if v >= cutoff}
        dropped = len(self.data) - len(pruned)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(pruned, indent=0, sort_keys=True))
        log.info("ledger: %d entries saved (%d pruned)", len(pruned), dropped)
