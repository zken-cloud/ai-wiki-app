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
    """Machine-readable export, for anything that wants the data outside the site."""
    path = wiki / "data" / "items" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "categories": {
            cat: [
                {
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


def rebuild_indexes(wiki: Path, categories: list[dict]) -> None:
    for cat in categories:
        category_index(wiki, cat)
    daily_index(wiki)
    home(wiki, categories)
