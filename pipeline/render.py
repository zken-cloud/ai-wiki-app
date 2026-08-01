"""Render collected+summarised items into the ai-wiki Markdown repo."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

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
    """Machine-readable export, and the recovery source for `load_existing`.

    Categories absent from `by_cat` are carried over from the existing file
    rather than dropped. Without this, a `--only <category>` run would rewrite
    the export with just that category, and the next full run would treat every
    other category as unpublished — losing their pages.
    """
    path = wiki / "data" / "items" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    prior_raw: dict[str, Any] = {}
    if path.exists():
        try:
            prior_raw = (json.loads(path.read_text()).get("categories") or {})
        except (json.JSONDecodeError, OSError) as e:
            log.error("cannot read %s (%s); not carrying anything over", path, e)

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
    # Carry over untouched categories verbatim.
    for cat, rows in prior_raw.items():
        payload["categories"].setdefault(cat, rows)
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
    """Refresh archive indexes.

    Deliberately does NOT touch index.md: that is the Signal brief, written by
    signal_page(). Only fall back to the generic home page if no brief exists
    yet (e.g. a fresh clone before the first run).
    """
    for cat in categories:
        category_index(wiki, cat)
    daily_index(wiki)
    if not (wiki / "docs" / "index.md").exists():
        home(wiki, categories)


# --------------------------------------------------------------------------- #
# Signal brief — the PWA's front page
# --------------------------------------------------------------------------- #

TAG_ICON = {
    "model": "🧠", "vendor": "📢", "security": "🔒",
    "infra": "⚙️", "research": "📄",
}


def signal_page(wiki: Path, date: str, sig: dict, counts: dict[str, int],
                categories: list[dict]) -> None:
    """Write docs/index.md as a one-screen brief.

    This is what opens when the PWA is launched, so it must stay short: the
    full per-category lists live one tap away.
    """
    picks = sig.get("picks") or []
    total = sum(counts.values())

    out = [
        "# Today's Signal",
        "",
        f"<small>{date} · {len(picks)} things worth knowing · "
        f"{total} items reviewed</small>",
        "",
    ]

    if not picks:
        out += [
            '!!! info "Quiet day"',
            "    Nothing cleared the bar today. That is a real signal too —",
            "    the full lists are still below if you want to look.",
            "",
        ]
    else:
        for n, p in enumerate(picks, 1):
            icon = TAG_ICON.get(p.get("tag"), "•")
            out += [
                f"### {icon} {n}. [{_esc(p['headline'])}]({p['url']})",
                "",
                f"{p.get('why','')}",
                "",
                f"<small>{_esc(p.get('source',''))} · `{p.get('tag','')}`</small>",
                "",
            ]

    out += ["---", "", "## Everything else", ""]
    for c in categories:
        n = counts.get(c["id"], 0)
        label = f"{n} today" if n else "nothing new"
        out += [f"- {c['icon']} **[{c['title']}]({c['id']}/index.md)** — <small>{label}</small>"]
    out += [
        "",
        f"- 🧠 **[Model Tracker](models.md)** — <small>what each lab currently ships</small>",
        f"- 📰 **[Full briefing for {date}](daily/{date}.md)** — <small>the long version</small>",
        "- 🗂️ **[All briefings](daily/index.md)**",
        "",
        "---",
        "",
        "<small>Search with the box above or press <kbd>/</kbd>. "
        "Install this as an app from your browser's menu to read offline.</small>",
        "",
    ]
    (wiki / "docs" / "index.md").write_text("\n".join(out))


# --------------------------------------------------------------------------- #
# Model tracker — state, not stream
# --------------------------------------------------------------------------- #

def _mkey(vendor: str, name: str) -> str:
    return f"{vendor.strip().lower()}|{name.strip().lower()}"


def update_models(wiki: Path, releases: list[dict]) -> dict:
    """Merge newly-detected releases into the cumulative tracker."""
    path = wiki / "data" / "models.json"
    store: dict[str, Any] = {"models": []}
    if path.exists():
        try:
            store = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("cannot read %s (%s); starting fresh", path, e)

    by_key = {_mkey(m["vendor"], m["model_name"]): m for m in store.get("models", [])}
    added = 0
    for r in releases:
        k = _mkey(r["vendor"], r["model_name"])
        if k in by_key:
            # Keep the earliest sighting, but let a later note/url win.
            prev = by_key[k]
            prev["date"] = min(prev.get("date") or r["date"], r["date"])
            if r.get("note"):
                prev["note"] = r["note"]
            prev["url"] = r.get("url") or prev.get("url")
        else:
            by_key[k] = dict(r)
            added += 1

    store["models"] = sorted(
        by_key.values(), key=lambda m: (m.get("date") or "", m["vendor"]), reverse=True
    )
    store["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, ensure_ascii=False))
    if added:
        log.info("model tracker: %d new entr%s", added, "y" if added == 1 else "ies")
    return store


def models_page(wiki: Path, store: dict) -> None:
    models = store.get("models", [])
    out = [
        "# 🧠 Model Tracker",
        "",
        "*Named model releases as they are detected. Newest first — this answers "
        "\"what does each lab currently ship?\" without scrolling the archive.*",
        "",
    ]
    if not models:
        out += ["_No releases detected yet. This fills in as launches are picked up._", ""]
    else:
        out += ["| Date | Vendor | Model | What changed | Source |", "|---|---|---|---|---|"]
        for m in models:
            note = _esc(m.get("note", ""))[:150]
            kind = m.get("kind", "release")
            badge = {"release": "", "update": " *(update)*", "deprecation": " *(deprecated)*"}.get(kind, "")
            out.append(
                f"| {m.get('date','')} | {_esc(m.get('vendor',''))} | "
                f"**{_esc(m.get('model_name',''))}**{badge} | {note} | "
                f"[link]({m.get('url','')}) |"
            )
        out += ["", f"<small>{len(models)} models tracked · updated {store.get('updated_at','')}</small>", ""]
    (wiki / "docs" / "models.md").write_text("\n".join(out))
