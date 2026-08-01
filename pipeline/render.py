"""Render collected+summarised items into the ai-wiki Markdown repo."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def _esc(text: str) -> str:
    """Neutralise characters that would break Markdown tables/links."""
    return (text or "").replace("|", "\\|").replace("[", "(").replace("]", ")").strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def _item_md(it: dict) -> str:
    s = it.get("_summary", {}) or {}
    x = it.get("extra", {}) or {}
    out = [f"### [{_esc(it['title'])}]({it['url']})", ""]

    meta = [f"**{_esc(it['source'])}**"]
    if it.get("published"):
        meta.append(it["published"])
    if x.get("upvotes"):
        meta.append(f"👍 {x['upvotes']}")
    if x.get("github_stars"):
        meta.append(f"⭐ {x['github_stars']}")
    if x.get("arxiv_id"):
        meta.append(f"`arXiv:{x['arxiv_id']}`")
    out += [" · ".join(meta), ""]

    if s.get("tldr"):
        out += [s["tldr"], ""]
    if s.get("key_points"):
        out += [f"- {_esc(p)}" for p in s["key_points"]] + [""]
    if s.get("why_it_matters"):
        out += [f"**Why it matters:** {s['why_it_matters']}", ""]
    if x.get("authors"):
        out += [f"<small>{_esc(', '.join(a for a in x['authors'] if a))}</small>", ""]
    if x.get("github_repo"):
        out += [f"<small>Code: [{_esc(x['github_repo'])}](https://github.com/{x['github_repo']})</small>", ""]
    if s.get("tags"):
        out += [" ".join(f"`{_slug(t)}`" for t in s["tags"]), ""]
    return "\n".join(out)


def category_page(wiki: Path, cat: dict, items: list[dict], date: str) -> Path | None:
    if not items:
        return None
    cid = cat["id"]
    body = [
        f"# {cat['icon']} {cat['title']} — {date}",
        "",
        f"*{cat['blurb']}*",
        "",
        f"{len(items)} item{'s' if len(items) != 1 else ''} selected.",
        "",
        "---",
        "",
    ]
    for it in items:
        body.append(_item_md(it))
        body.append("---")
        body.append("")
    path = wiki / "docs" / cid / f"{date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body))
    return path


def daily_page(wiki: Path, date: str, digest_md: str, counts: dict[str, int]) -> Path:
    total = sum(counts.values())
    rows = "\n".join(
        f"| {c} | {n} | [{date} →]({{}}/{date}.md) |".replace("{}", f"../{c}")
        for c, n in counts.items() if n
    )
    body = f"""# 📰 Daily Briefing — {date}

*{total} items across {sum(1 for n in counts.values() if n)} categories.*

{digest_md}

---

## Today's pages

| Category | Items | Full list |
|---|---:|---|
{rows}
"""
    path = wiki / "docs" / "daily" / f"{date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _dates_in(dirpath: Path) -> list[str]:
    if not dirpath.exists():
        return []
    dates = [
        p.stem for p in dirpath.glob("*.md")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)
    ]
    return sorted(dates, reverse=True)


def category_index(wiki: Path, cat: dict) -> None:
    cid = cat["id"]
    dates = _dates_in(wiki / "docs" / cid)
    lines = [
        f"# {cat['icon']} {cat['title']}",
        "",
        f"*{cat['blurb']}*",
        "",
        "## Archive",
        "",
    ]
    if dates:
        by_month: dict[str, list[str]] = {}
        for d in dates:
            by_month.setdefault(d[:7], []).append(d)
        for month, ds in by_month.items():
            label = dt.date.fromisoformat(f"{month}-01").strftime("%B %Y")
            lines += [f"**{label}**", ""]
            lines += [f"- [{d}]({d}.md)" for d in ds]
            lines += [""]
    else:
        lines += ["_Nothing collected yet — the first run populates this page._", ""]
    (wiki / "docs" / cid / "index.md").write_text("\n".join(lines))


def home(wiki: Path, categories: list[dict]) -> None:
    daily = _dates_in(wiki / "docs" / "daily")
    latest = daily[0] if daily else None

    lines = [
        "# AI Wiki",
        "",
        "An automatically curated, daily-updated digest of AI research, security, "
        "infrastructure, and lab announcements. Everything here is collected from "
        "public sources, triaged for relevance, and summarised.",
        "",
    ]
    if latest:
        lines += [
            f"## 📰 [Latest briefing — {latest}](daily/{latest}.md)",
            "",
        ]
    lines += ["## Categories", ""]
    for c in categories:
        n = len(_dates_in(wiki / "docs" / c["id"]))
        lines.append(f"- {c['icon']} **[{c['title']}]({c['id']}/index.md)** — {c['blurb']} <small>({n} days archived)</small>")
    lines += [
        "",
        "## Recent briefings",
        "",
    ]
    lines += [f"- [{d}](daily/{d}.md)" for d in daily[:14]] or ["_None yet._"]
    lines += [
        "",
        "---",
        "",
        "<small>Use the search box (or press <kbd>/</kbd>) to search every page. "
        "Built by the pipeline in `zken-cloud/ai-wiki-app`.</small>",
        "",
    ]
    (wiki / "docs" / "index.md").write_text("\n".join(lines))


def daily_index(wiki: Path) -> None:
    dates = _dates_in(wiki / "docs" / "daily")
    lines = ["# 📰 Daily Briefings", ""]
    if dates:
        lines += [f"- [{d}]({d}.md)" for d in dates]
    else:
        lines += ["_Nothing yet._"]
    (wiki / "docs" / "daily" / "index.md").write_text("\n".join(lines))


def export_json(wiki: Path, date: str, by_cat: dict[str, list[dict]]) -> None:
    """Machine-readable export, and the recovery source for `load_existing`."""
    path = wiki / "data" / "items" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "categories": {
            cat: [
                {
                    "uid": it.get("uid", ""),
                    "title": it["title"], "url": it["url"], "source": it["source"],
                    "published": it.get("published", ""), "score": it.get("_score"),
                    "summary": it.get("_summary", {}), "extra": it.get("extra", {}),
                }
                for it in items
            ]
            for cat, items in by_cat.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def load_existing(wiki: Path, date: str) -> dict[str, list[dict]]:
    """Re-hydrate items already published for `date` from the JSON export.

    Without this, a second run on the same date would rewrite that date's pages
    using only the items the dedupe ledger considers *new* — silently deleting
    everything published earlier that day.
    """
    path = wiki / "data" / "items" / f"{date}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("cannot read %s (%s); treating as empty", path, e)
        return {}

    out: dict[str, list[dict]] = {}
    for cat, rows in (data.get("categories") or {}).items():
        out[cat] = [
            {
                "uid": r.get("uid", ""),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source": r.get("source", ""),
                "published": r.get("published", ""),
                "body": "",
                "category": cat,
                "_score": r.get("score"),
                "_summary": r.get("summary") or {},
                "extra": r.get("extra") or {},
            }
            for r in rows if r.get("title")
        ]
    return out


def merge_items(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Union of already-published and newly-selected items, newest score first.

    De-duplicated on URL (stable across runs and present in the export).
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for it in [*existing, *fresh]:
        key = it.get("url") or it.get("uid") or it.get("title", "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
    merged.sort(
        key=lambda it: (
            it.get("_score") or 0,
            (it.get("extra") or {}).get("upvotes", 0),
            it.get("published", ""),
        ),
        reverse=True,
    )
    return merged


def rebuild_indexes(wiki: Path, categories: list[dict]) -> None:
    for cat in categories:
        category_index(wiki, cat)
    daily_index(wiki)
    home(wiki, categories)
