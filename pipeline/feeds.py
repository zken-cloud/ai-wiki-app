"""Atom feeds, generated from the JSON the pipeline already writes.

No new dependency and no model call: everything here is a re-projection of
`data/signal/<date>.json` and `data/items/<date>.json`.

Two kinds of feed, because they answer different questions:
  * /feed.xml            — the curated brief. Signal picks + papers only.
  * /<category>/feed.xml — everything published in one category.

Feed-level <updated> is derived from the newest entry, never from the wall
clock. If nothing new was published, the file is byte-identical to last run's
and produces no commit — the pipeline commits whatever changed, so a clock in
here would create a diff every single day.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger(__name__)

SITE = "https://zken-cloud.github.io/ai-wiki"
WINDOW_DAYS = 30
# 100 keeps the biggest feed (papers, ~25/day) around 100KB. The file is
# regenerated and committed every run, so an unbounded feed would grow the
# repo daily for entries no reader will scroll to.
MAX_ENTRIES = 100


def _ts(date: str) -> str:
    """A date string as an Atom timestamp.

    Entries carry day granularity only, so pick a fixed time of day rather
    than the run time — same reasoning as the module docstring.
    """
    return f"{date}T08:00:00Z"


def _recent_dates(dirpath: Path, days: int = WINDOW_DAYS) -> list[str]:
    if not dirpath.exists():
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    dates = [p.stem for p in dirpath.glob("*.json") if p.stem >= cutoff]
    return sorted(dates, reverse=True)


def _entry(title: str, url: str, updated: str, summary: str,
           source: str = "", categories: list[str] | None = None,
           published: str = "") -> list[str]:
    out = [
        "  <entry>",
        f"    <title>{escape(title)}</title>",
        f"    <link rel=\"alternate\" href={quoteattr(url)}/>",
        f"    <id>{escape(url)}</id>",
        f"    <updated>{updated}</updated>",
    ]
    # <updated> is when the wiki surfaced it (what a reader sorts by);
    # <published> is the original date, which can be much earlier.
    if published and published != updated:
        out.append(f"    <published>{published}</published>")
    for c in categories or []:
        out.append(f"    <category term={quoteattr(c)}/>")
    if source:
        out.append(f"    <author><name>{escape(source)}</name></author>")
    if summary:
        out.append(f"    <summary type=\"html\">{escape(summary)}</summary>")
    out.append("  </entry>")
    return out


def _finalise(entries: list[tuple[str, list[str]]]) -> tuple[list[list[str]], str | None]:
    """Newest first, then cap.

    Sorting before capping matters: an item's timestamp is its *publication*
    date, which can be older than the date-file it was collected into, so
    first-encountered order is not chronological. Capping unsorted would drop
    newer entries in favour of older ones.
    """
    entries.sort(key=lambda e: e[0], reverse=True)
    kept = entries[:MAX_ENTRIES]
    return [e[1] for e in kept], (kept[0][0] if kept else None)


def _feed(path: Path, feed_id: str, title: str, subtitle: str,
          self_href: str, alt_href: str, entries: list[list[str]],
          newest: str | None) -> None:
    if not newest:
        newest = _ts(dt.date.today().isoformat())
    head = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <id>{escape(feed_id)}</id>",
        f"  <title>{escape(title)}</title>",
        f"  <subtitle>{escape(subtitle)}</subtitle>",
        f"  <updated>{newest}</updated>",
        f"  <link rel=\"self\" href={quoteattr(self_href)}/>",
        f"  <link rel=\"alternate\" href={quoteattr(alt_href)}/>",
        "  <generator>ai-wiki</generator>",
    ]
    body = [line for e in entries for line in e]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head + body + ["</feed>", ""]))


def signal_feed(wiki: Path) -> int:
    """The curated feed: each day's Signal picks and papers, newest first."""
    rows: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for date in _recent_dates(wiki / "data" / "signal"):
        try:
            sig = json.loads((wiki / "data" / "signal" / f"{date}.json").read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("signal feed: skipping %s (%s)", date, e)
            continue
        for p in (sig.get("picks") or []) + (sig.get("papers") or []):
            url = p.get("url")
            # The same story can be picked on consecutive days; the feed should
            # carry it once, at its first appearance.
            if not url or url in seen:
                continue
            seen.add(url)
            ts = _ts(date)
            rows.append((ts, _entry(
                p.get("headline") or p.get("title", ""), url, ts,
                p.get("why", ""), p.get("source", ""),
                [p.get("tag", "")] if p.get("tag") else [],
            )))
    entries, newest = _finalise(rows)
    _feed(
        wiki / "docs" / "feed.xml",
        f"{SITE}/feed.xml",
        "AI Wiki — Daily Signal",
        "The day's most important AI model releases, lab announcements, and security news.",
        f"{SITE}/feed.xml", f"{SITE}/",
        entries, newest,
    )
    return len(entries)


def category_feed(wiki: Path, cat: dict) -> int:
    """One feed per category: everything published there, newest first."""
    cid = cat["id"]
    rows: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for date in _recent_dates(wiki / "data" / "items"):
        try:
            data = json.loads((wiki / "data" / "items" / f"{date}.json").read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("%s feed: skipping %s (%s)", cid, date, e)
            continue
        for it in (data.get("categories") or {}).get(cid) or []:
            url = it.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            s = it.get("summary") or {}
            body = s.get("tldr", "")
            if s.get("why_it_matters"):
                body = f"{body}<br/><br/><em>Why it matters:</em> {s['why_it_matters']}"
            # Order by the date the wiki surfaced the item, NOT by its own
            # publication date. Hugging Face routinely features papers weeks
            # after submission (measured: up to 76 days), and arXiv items are
            # collected over a 7-day window. Sorting by `published` pushed
            # 86 of 168 recently-surfaced papers below the 100-entry cap, so
            # half of the newest content never reached the feed at all.
            ts = _ts(date)
            orig = it.get("published") or ""
            rows.append((ts, _entry(
                it.get("title", ""), url, ts,
                body, it.get("source", ""), s.get("tags") or [],
                published=_ts(orig[:10]) if len(orig) >= 10 else "",
            )))
    entries, newest = _finalise(rows)
    _feed(
        wiki / "docs" / cid / "feed.xml",
        f"{SITE}/{cid}/feed.xml",
        f"AI Wiki — {cat['title']}",
        cat.get("blurb", ""),
        f"{SITE}/{cid}/feed.xml", f"{SITE}/{cid}/",
        entries, newest,
    )
    return len(entries)


def build_all(wiki: Path, categories: list[dict]) -> None:
    n = signal_feed(wiki)
    log.info("feed: signal -> %d entries", n)
    for cat in categories:
        log.info("feed: %s -> %d entries", cat["id"], category_feed(wiki, cat))
