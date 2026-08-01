#!/usr/bin/env python3
"""ai-wiki pipeline orchestrator.

    python run.py --wiki ../wiki                 # full run
    python run.py --wiki ../wiki --dry-run       # collect + triage, no writes, no summaries
    python run.py --wiki ../wiki --only papers   # one category
    python run.py --check                        # verify Vertex auth only
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import yaml

from pipeline import collect as collect_mod
from pipeline import enrich, llm, render, state

log = logging.getLogger("ai-wiki")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the AI wiki for one day.")
    ap.add_argument("--wiki", type=Path, help="path to the ai-wiki content repo")
    ap.add_argument("--config", type=Path, default=Path(__file__).parent / "sources.yaml")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--only", action="append", help="restrict to category id(s)")
    ap.add_argument("--dry-run", action="store_true", help="no LLM summaries, no file writes")
    ap.add_argument("--check", action="store_true", help="probe Vertex AI credentials and exit")
    ap.add_argument("--no-dedupe", action="store_true", help="ignore the seen-ledger")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.check:
        ok = llm.healthcheck()
        print(("PASS" if ok else "FAIL") + f" — Vertex AI ({llm.PROJECT}/{llm.LOCATION})")
        return 0 if ok else 1

    if not args.wiki:
        ap.error("--wiki is required (unless --check)")

    cfg = yaml.safe_load(args.config.read_text())
    defaults = cfg.get("defaults", {})
    models = defaults.get("models", {})
    window = defaults.get("window_days", 3)
    concurrency = defaults.get("concurrency", 6)

    categories = cfg["categories"]
    if args.only:
        wanted = set(args.only)
        categories = [c for c in categories if c["id"] in wanted]
        if not categories:
            log.error("no categories matched %s", sorted(wanted))
            return 2

    wiki: Path = args.wiki.resolve()
    if not args.dry_run and not wiki.is_dir():
        log.error("wiki path does not exist: %s", wiki)
        return 2

    ledger = state.Seen(wiki / "data" / "seen.json")
    date = args.date
    by_cat: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}

    for cat in categories:
        cid = cat["id"]
        log.info("=== category: %s ===", cid)

        items = collect_mod.collect(cat.get("sources", []), window, cid)
        if not args.no_dedupe:
            items = ledger.filter_new(items)
        if not items:
            log.info("%s: nothing new", cid)
            by_cat[cid], counts[cid] = [], 0
            continue

        selected = enrich.select(items, cat.get("filter", {}), cat.get("criteria", ""), models)
        if not selected:
            log.info("%s: nothing cleared the relevance bar", cid)
            by_cat[cid], counts[cid] = [], 0
            continue

        if args.dry_run:
            for it in selected:
                log.info("  [%s] %-58s %s", it.get("_score"), it["title"][:58], it["source"])
        else:
            enrich.summarise(selected, models.get("summarize", "gemini-2.5-flash"), concurrency)
            render.category_page(wiki, cat, selected, date)

        by_cat[cid], counts[cid] = selected, len(selected)

    total = sum(counts.values())
    log.info("=== total selected: %d ===", total)

    if args.dry_run:
        log.info("dry run — no files written, no ledger update")
        return 0

    if total:
        titled = {
            next(c["title"] for c in categories if c["id"] == cid): items
            for cid, items in by_cat.items() if items
        }
        digest_md = enrich.digest(titled, date, models.get("digest", "gemini-2.5-pro"))
        render.daily_page(wiki, date, digest_md, counts)
        render.export_json(wiki, date, by_cat)
        for items in by_cat.values():
            ledger.mark(items, date)
        ledger.save()
    else:
        log.info("no items today — refreshing indexes only")

    render.rebuild_indexes(wiki, cfg["categories"])
    log.info("done: %s", wiki)
    return 0


if __name__ == "__main__":
    sys.exit(main())
