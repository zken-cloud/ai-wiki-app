"""LLM stages: relevance triage -> per-item summary -> daily digest.

Cost control, cheapest first:
  1. Keyword prefilter        (free)
  2. Curated bypass           (free — HF upvotes already signal relevance)
  3. Batched scoring on Lite  (~20 items per call)
  4. Per-item summary on Flash (only survivors)
  5. One digest call on Pro
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
from typing import Any

from . import llm

log = logging.getLogger(__name__)

SCORE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"i": {"type": "INTEGER"}, "s": {"type": "INTEGER"}},
        "required": ["i", "s"],
    },
}

SUMMARY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tldr": {"type": "STRING"},
        "key_points": {"type": "ARRAY", "items": {"type": "STRING"}},
        "why_it_matters": {"type": "STRING"},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["tldr", "key_points", "why_it_matters", "tags"],
}


# --------------------------------------------------------------------------- #
# 1-3. Filtering
# --------------------------------------------------------------------------- #

def keyword_prefilter(items: list[dict], keywords: list[str]) -> list[dict]:
    if not keywords:
        return items
    lowered = [k.lower() for k in keywords]
    kept = [
        it for it in items
        if any(k in f"{it['title']} {it['body']}".lower() for k in lowered)
    ]
    log.info("keyword prefilter: %d -> %d", len(items), len(kept))
    return kept


def score_items(items: list[dict], criteria: str, model: str, batch: int = 20) -> None:
    """Attach `_score` (1-5) to each item, in place."""
    todo = [it for it in items if "_score" not in it]
    if not todo:
        return

    def run_batch(chunk: list[dict]) -> None:
        listing = "\n".join(
            f"[{n}] {it['title']}\n    {it['body'][:320]}"
            for n, it in enumerate(chunk)
        )
        prompt = (
            f"Relevance criteria:\n{criteria}\n\n"
            "Score EACH item 1-5 for how well it matches the criteria "
            "(5 = squarely on-topic and substantive, 1 = off-topic or pure noise).\n"
            "Return one object per item using its bracketed index.\n\n"
            f"Items:\n{listing}"
        )
        try:
            rows = llm.generate(
                prompt,
                model=model,
                schema=SCORE_SCHEMA,
                system="You are a precise, sceptical technical editor triaging a firehose.",
                max_tokens=4096,
                temperature=0.0,
                thinking_budget=0,
            )
        except Exception as e:  # noqa: BLE001 - never let triage sink the run
            log.error("scoring batch failed (%s); defaulting to keep", e)
            for it in chunk:
                it["_score"] = 3
            return
        by_idx = {int(r["i"]): int(r["s"]) for r in rows if "i" in r and "s" in r}
        for n, it in enumerate(chunk):
            it["_score"] = max(1, min(5, by_idx.get(n, 3)))

    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    with cf.ThreadPoolExecutor(min(4, max(1, len(chunks)))) as ex:
        list(ex.map(run_batch, chunks))


def select(items: list[dict], cfg: dict, criteria: str, models: dict) -> list[dict]:
    """Full triage funnel for one category."""
    kept = keyword_prefilter(items, cfg.get("keywords", []))

    if cfg.get("always_keep_curated"):
        for it in kept:
            if it.get("extra", {}).get("curated"):
                it["_score"] = 5  # community-upvoted: skip the LLM entirely

    score_items(kept, criteria, models["score"])

    floor = cfg.get("min_score", 4)
    winners = [it for it in kept if it.get("_score", 0) >= floor]

    def rank(it: dict) -> tuple:
        return (
            it.get("_score", 0),
            it.get("extra", {}).get("upvotes", 0),
            it.get("published", ""),
        )

    winners.sort(key=rank, reverse=True)
    cap = cfg.get("max_items", 20)
    max_curated = cfg.get("max_curated")

    if max_curated is None:
        capped = winners[:cap]
    else:
        # Curated items are pinned to score 5, so without a quota they would
        # consume the entire cap and starve the tier-2 recall feed.
        curated = [it for it in winners if it.get("extra", {}).get("curated")]
        others = [it for it in winners if not it.get("extra", {}).get("curated")]
        picked = curated[:max_curated]
        picked += others[: max(0, cap - len(picked))]
        # Backfill from curated if tier-2 under-delivered, so we never waste slots.
        if len(picked) < cap:
            already = {it["uid"] for it in picked}
            picked += [it for it in curated if it["uid"] not in already][: cap - len(picked)]
        picked.sort(key=rank, reverse=True)
        capped = picked

    n_cur = sum(1 for it in capped if it.get("extra", {}).get("curated"))
    log.info(
        "select: %d collected -> %d prefiltered -> %d scored>=%d -> %d kept (%d curated / %d other)",
        len(items), len(kept), len(winners), floor, len(capped), n_cur, len(capped) - n_cur,
    )
    return capped


# --------------------------------------------------------------------------- #
# 4. Summaries
# --------------------------------------------------------------------------- #

SUMMARY_SYSTEM = (
    "You write dense, factual briefings for an expert technical reader. "
    "No hype, no filler, no restating the title. Prefer concrete specifics "
    "(numbers, method names, model names) over vague claims. If the source "
    "text is thin, say less rather than inventing detail."
)


def summarise(items: list[dict], model: str, concurrency: int = 6) -> None:
    """Attach `_summary` to each item, in place."""

    def one(it: dict) -> None:
        prompt = (
            f"Title: {it['title']}\n"
            f"Source: {it['source']}\n"
            f"URL: {it['url']}\n\n"
            f"Content:\n{it['body'][:6000]}\n\n"
            "Produce:\n"
            "- tldr: one sentence, max 40 words, stating what is new.\n"
            "- key_points: 2-5 bullets of specific technical substance.\n"
            "- why_it_matters: one sentence on the practical implication.\n"
            "- tags: 2-5 short lowercase topic tags.\n"
            "Base everything strictly on the content above."
        )
        try:
            it["_summary"] = llm.generate(
                prompt, model=model, schema=SUMMARY_SCHEMA,
                system=SUMMARY_SYSTEM, max_tokens=4096, temperature=0.2,
                thinking_budget=0,
            )
        except Exception as e:  # noqa: BLE001
            log.error("summary failed for %r: %s", it["title"][:60], e)
            it["_summary"] = {
                "tldr": it["body"][:220] or it["title"],
                "key_points": [],
                "why_it_matters": "",
                "tags": [],
                "_degraded": True,
            }

    with cf.ThreadPoolExecutor(concurrency) as ex:
        list(ex.map(one, items))


# --------------------------------------------------------------------------- #
# 5. Daily digest
# --------------------------------------------------------------------------- #

def digest(by_category: dict[str, list[dict]], date: str, model: str) -> str:
    """One cross-category narrative digest. Returns markdown."""
    blocks: list[str] = []
    for cat, items in by_category.items():
        if not items:
            continue
        lines = [
            f"- [{it['title']}]({it['url']}) — {it.get('_summary', {}).get('tldr', '')}"
            for it in items[:12]
        ]
        blocks.append(f"## {cat}\n" + "\n".join(lines))

    if not blocks:
        return "_No qualifying items were collected for this date._\n"

    prompt = (
        f"Below are today's ({date}) selected AI items, grouped by category.\n\n"
        + "\n\n".join(blocks)
        + "\n\nWrite a briefing in Markdown with exactly these sections:\n"
        "### The short version\n"
        "3-5 bullets covering the most consequential items overall.\n\n"
        "### Themes\n"
        "1-3 short paragraphs connecting items to each other. Only claim a "
        "theme if the items genuinely support it; if the day is scattered, "
        "say so plainly instead of manufacturing a narrative.\n\n"
        "### Worth a closer look\n"
        "2-4 bullets, each naming one item and why it rewards a full read.\n\n"
        "Link items as [title](url) when you mention them. Do not add any "
        "other sections, and do not invent items that are not listed above."
    )
    try:
        return llm.generate(
            prompt, model=model,
            system="You are the editor of a respected daily AI briefing. Be concise and sceptical.",
            # Generous ceiling: 2.5-pro cannot disable thinking, and thinking
            # tokens bill against maxOutputTokens. At 3000 the digest was
            # truncated mid-sentence.
            max_tokens=16000, temperature=0.3,
        )
    except Exception as e:  # noqa: BLE001
        log.error("digest failed: %s", e)
        return "_Digest generation failed for this run; per-category pages below are unaffected._\n"
