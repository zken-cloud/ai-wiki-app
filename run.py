#!/usr/bin/env python3
"""ai-wiki pipeline orchestrator.

    python run.py --wiki ../wiki                 # today
    python run.py --wiki ../wiki --backfill 30   # rebuild the last 30 days
    python run.py --wiki ../wiki --dry-run       # collect + triage only
    python run.py --wiki ../wiki --only papers   # one category
    python run.py --check                        # verify Vertex auth only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import yaml

from pipeline import collect as collect_mod
from pipeline import benchmarks as bench_mod
from pipeline import enrich, feeds, llm, render, state

log = logging.getLogger("ai-wiki")


def build_day(
    date: str,
    categories: list[dict],
    wiki: Path,
    ledger: state.Seen,
    models: dict,
    window: int,
    concurrency: int,
    dry_run: bool,
    dedupe: bool,
    prefetched: dict[str, dict] | None = None,
) -> int:
    """Build one date. Returns the number of items published."""
    by_cat: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}

    # Anything already published for this date. Re-running a date must ADD to
    # its pages, never replace them with just the newly-discovered items.
    existing = {} if dry_run else render.load_existing(wiki, date)
    if existing:
        log.info("%s: %d item(s) already published, will merge",
                 date, sum(len(v) for v in existing.values()))

    # Items already taken by an earlier category *in this same run*, or already
    # published for this date by any category.
    #
    # The ledger alone cannot cover this: it is only written at the end of the
    # day (below), so while the category loop is running every category sees a
    # ledger that predates the whole run. arXiv papers cross-listed to both
    # cs.CR and cs.LG were therefore collected by `papers` (via cs.LG) and by
    # `security` (via cs.CR) and published twice — 25 such duplicates were
    # found in an audit of the first month.
    #
    # Keyed on URL, matching render.merge_items, because URL is stable and
    # always present while re-hydrated rows can carry a blank uid.
    def _key(it: dict) -> str:
        return it.get("url") or it.get("uid") or it.get("title", "")

    claimed: set[str] = {
        _key(it) for items in existing.values() for it in items
    }

    for cat in categories:
        cid = cat["id"]
        sources = cat.get("sources", [])

        if prefetched is not None:
            items = collect_mod.collect_for_date(
                sources, window, cid, date, prefetched.get(cid, {})
            )
        else:
            items = collect_mod.collect(sources, window, cid)

        prior = existing.get(cid, [])

        if dedupe:
            items = ledger.filter_new(items)
        if dedupe and claimed:
            before = len(items)
            items = [it for it in items if _key(it) not in claimed]
            if before != len(items):
                log.info("cross-category: %d -> %d (claimed earlier this run)",
                         before, len(items))
        if not items:
            by_cat[cid], counts[cid] = prior, len(prior)
            continue

        selected = enrich.select(items, cat.get("filter", {}), cat.get("criteria", ""), models)
        if not selected:
            by_cat[cid], counts[cid] = prior, len(prior)
            continue

        # Claim before summarising, so a later category never pays to
        # summarise something this one already took.
        claimed.update(_key(it) for it in selected)

        if dry_run:
            for it in selected:
                log.info("  [%s] %-56s %s", it.get("_score"), it["title"][:56], it["source"])
        else:
            enrich.summarise(selected, models.get("summarize", "gemini-3.6-flash"), concurrency)
            selected = render.merge_items(prior, selected)
            render.category_page(wiki, cat, selected, date)

        by_cat[cid], counts[cid] = selected, len(selected)

    total = sum(counts.values())
    if dry_run or not total:
        return total
    # Nothing new: skip the expensive digest, but still ensure the Signal
    # brief exists — it is the homepage and the PWA's launch target, so it
    # must never be missing or stale for the current date.
    nothing_new = bool(existing) and total == sum(len(v) for v in existing.values())
    signal_path = wiki / "data" / "signal" / f"{date}.json"
    if nothing_new and signal_path.exists():
        log.info("%s: nothing new, pages left as-is", date)
        return total

    titled = {
        next(c["title"] for c in categories if c["id"] == cid): items
        for cid, items in by_cat.items() if items
    }
    if not nothing_new:
        digest_md = enrich.digest(titled, date, models.get("digest", "gemini-3.6-flash"))
        render.daily_page(wiki, date, digest_md, counts)
        render.export_json(wiki, date, by_cat)

    # --- focus layer: one-screen brief + model tracker ---
    flat = [it for items in by_cat.values() for it in items]

    sig = enrich.signal(flat, date, models.get("signal", models.get("digest")))
    render.signal_page(wiki, date, sig, counts, categories)
    (wiki / "data" / "signal").mkdir(parents=True, exist_ok=True)
    (wiki / "data" / "signal" / f"{date}.json").write_text(
        json.dumps({"date": date, **sig}, indent=2, ensure_ascii=False)
    )

    # Only vendor/infra/security items can plausibly announce a model.
    candidates = [it for it in flat if it.get("category") in ("vendors", "infra", "security")]
    releases = enrich.extract_releases(candidates, date, models.get("score"))
    render.models_page(wiki, render.update_models(wiki, releases))

    for items in by_cat.values():
        ledger.mark(items, date)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the AI wiki.")
    ap.add_argument("--wiki", type=Path, help="path to the ai-wiki content repo")
    ap.add_argument("--config", type=Path, default=Path(__file__).parent / "sources.yaml")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="rebuild the last N days (oldest first), not just --date")
    ap.add_argument("--only", action="append", help="restrict to category id(s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-dedupe", action="store_true")
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
    window = defaults.get("window_days", 7)
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
    dedupe = not args.no_dedupe
    grand_total = 0

    if args.backfill:
        end = dt.date.fromisoformat(args.date)
        dates = [(end - dt.timedelta(days=n)).isoformat()
                 for n in range(args.backfill - 1, -1, -1)]  # oldest first
        log.info("backfill: %s .. %s (%d days)", dates[0], dates[-1], len(dates))

        # Feeds are date-agnostic: fetch each once, bucket by publish date.
        log.info("prefetching feed/sitemap sources once…")
        prefetched: dict[str, dict] = {}
        for cat in categories:
            prefetched[cat["id"]] = collect_mod.prefetch_undated(
                cat.get("sources", []), args.backfill + 5
            )

        for n, date in enumerate(dates, 1):
            log.info("===== [%d/%d] %s =====", n, len(dates), date)
            got = build_day(date, categories, wiki, ledger, models, window,
                            concurrency, args.dry_run, dedupe, prefetched)
            grand_total += got
            log.info("[%d/%d] %s -> %d items", n, len(dates), date, got)
    else:
        log.info("===== %s =====", args.date)
        grand_total = build_day(args.date, categories, wiki, ledger, models, window,
                                concurrency, args.dry_run, dedupe)

    log.info("=== total published: %d ===", grand_total)
    log.info("token usage:\n%s", llm.usage_report())

    if args.dry_run:
        log.info("dry run — no files written, no ledger update")
        return 0

    if grand_total:
        ledger.save()

    # Leaderboards are state, not daily content — refresh every run, even when
    # no new articles were found.
    if cfg.get("benchmarks"):
        boards = bench_mod.fetch_all(cfg["benchmarks"])
        render.benchmarks_page(wiki, boards)
        render.export_benchmarks(wiki, boards)

    render.rebuild_indexes(wiki, cfg["categories"])
    feeds.build_all(wiki, cfg["categories"])
    log.info("done: %s", wiki)
    return 0


if __name__ == "__main__":
    sys.exit(main())
