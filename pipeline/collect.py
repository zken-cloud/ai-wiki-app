"""Source adapters. Each yields a normalised Item dict.

Adding a new source type = add one function + register it in ADAPTERS.
Adding a new *source* of an existing type = edit sources.yaml only.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import logging
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Iterable

import feedparser
import requests

log = logging.getLogger(__name__)

UA = {"User-Agent": "ai-wiki/1.0 (+https://github.com/zken-cloud/ai-wiki)"}
TIMEOUT = 45
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# arXiv asks API clients to leave ~3s between requests, but 3s is not enough
# for a backfill: 30 days x 4 categories x max_results=60 is a sustained burst,
# and arXiv 429s partway through — whole days then silently lose their paper
# coverage, which is worse than a slow run. 8s survives a full 30-day backfill.
# A normal daily run makes 4 calls, so this costs it ~30s.
ARXIV_MIN_INTERVAL = 8.0
_arxiv_lock = threading.Lock()
_arxiv_last = 0.0


def _arxiv_get(params: dict, attempts: int = 6) -> requests.Response:
    """Rate-limited, retrying GET against the arXiv API.

    Six attempts, not four: a 429 partway through a backfill is recoverable if
    we simply wait longer, and losing a day's papers is not.
    """
    global _arxiv_last
    last_err = ""
    for attempt in range(attempts):
        r = None
        with _arxiv_lock:
            wait = ARXIV_MIN_INTERVAL - (time.monotonic() - _arxiv_last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.get(
                    "https://export.arxiv.org/api/query",
                    params=params, headers=UA, timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
            finally:
                _arxiv_last = time.monotonic()
        if r is not None and r.status_code == 200:
            return r
        backoff = ARXIV_MIN_INTERVAL * (2 ** attempt) + random.uniform(0, 1.0)
        if r is not None:
            last_err = f"HTTP {r.status_code}"
            # If arXiv tells us how long to wait, believe it over our guess.
            try:
                backoff = max(backoff, float(r.headers.get("Retry-After", 0)))
            except ValueError:
                pass
        log.warning("arxiv retry %d/%d in %.1fs (%s)", attempt + 1, attempts, backoff, last_err)
        time.sleep(backoff)
    raise RuntimeError(f"arxiv unavailable after {attempts} attempts: {last_err}")


def _uid(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _clean(html: str | None, limit: int = 4000) -> str:
    if not html:
        return ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _iso(value: Any) -> str:
    """Best-effort normalisation to YYYY-MM-DD."""
    if not value:
        return ""
    if isinstance(value, str):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return m.group(0)
        try:
            return dt.datetime.strptime(value[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
        except ValueError:
            return ""
    try:  # feedparser struct_time
        return dt.date(value[0], value[1], value[2]).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _recent(iso_date: str, days: int) -> bool:
    """Keep undated items (better a false positive than silently dropping news)."""
    if not iso_date:
        return True
    try:
        d = dt.date.fromisoformat(iso_date)
    except ValueError:
        return True
    return (dt.date.today() - d).days <= days


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #

def hf_daily(cfg: dict, window: int, date: str | None = None) -> list[dict]:
    """Hugging Face curated daily papers — carries community upvotes.

    Supports historical retrieval: `?date=YYYY-MM-DD` returns that day's
    curated set, which is what makes paper backfill possible.
    """
    out: list[dict] = []
    params: dict[str, Any] = {"limit": cfg.get("limit", 40)}
    if date:
        params["date"] = date
    try:
        r = requests.get(
            "https://huggingface.co/api/daily_papers",
            params=params,
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("hf_daily failed: %s", e)
        return out

    for row in rows:
        p = row.get("paper", {}) or {}
        arxiv_id = p.get("id") or ""
        title = (row.get("title") or p.get("title") or "").strip()
        if not title:
            continue
        published = _iso(row.get("publishedAt") or p.get("publishedAt"))
        if date:
            published = published or date
        elif not _recent(published, window):
            continue
        out.append({
            "uid": _uid("hf", arxiv_id or title),
            "title": title,
            "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else row.get("thumbnail", ""),
            "source": "Hugging Face Daily Papers",
            "published": published,
            "body": _clean(row.get("summary") or p.get("summary")),
            "extra": {
                "arxiv_id": arxiv_id,
                "upvotes": p.get("upvotes") or 0,
                "github_repo": p.get("githubRepo") or "",
                "github_stars": p.get("githubStars") or 0,
                "authors": [a.get("name", "") for a in (p.get("authors") or [])][:8],
                "curated": True,
            },
        })
    log.info("hf_daily: %d items", len(out))
    return out


def arxiv(cfg: dict, window: int, date: str | None = None) -> list[dict]:
    """arXiv Atom API, one query per category.

    With `date`, restricts to that submission day via a submittedDate range —
    this is what lets the archive be backfilled. NOTE: the range must be passed
    through `params=` so requests encodes the brackets; hand-built query strings
    silently return the newest papers instead of the requested window.
    """
    out: list[dict] = []
    per_cat = cfg.get("max_results_per_category", 60)
    for cat in cfg.get("categories", []):
        query = f"cat:{cat}"
        if date:
            d = date.replace("-", "")
            nxt = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat().replace("-", "")
            query = f"cat:{cat} AND submittedDate:[{d}0000 TO {nxt}0000]"
        try:
            r = _arxiv_get({
                "search_query": query,
                "max_results": per_cat,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            })
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            log.error("arxiv %s failed: %s", cat, e)
            continue

        for e in feed.entries:
            published = _iso(getattr(e, "published_parsed", None)) or _iso(getattr(e, "published", ""))
            if date:
                published = published or date
            elif not _recent(published, window):
                continue
            link = getattr(e, "link", "")
            aid = link.rsplit("/", 1)[-1] if link else ""
            out.append({
                "uid": _uid("arxiv", aid or e.get("title", "")),
                "title": _clean(e.get("title"), 400),
                "url": link,
                "source": f"arXiv {cat}",
                "published": published,
                "body": _clean(e.get("summary")),
                "extra": {
                    "arxiv_id": aid,
                    "arxiv_category": cat,
                    "authors": [a.get("name", "") for a in (e.get("authors") or [])][:8],
                    "curated": False,
                },
            })
        log.info("arxiv %s: running total %d", cat, len(out))
    return out


def rss(cfg: dict, window: int) -> list[dict]:
    """Generic RSS/Atom via feedparser (tolerant of malformed feeds)."""
    out: list[dict] = []
    limit = cfg.get("limit_per_feed", 25)

    def one(feed_cfg: dict) -> list[dict]:
        name, url = feed_cfg["name"], feed_cfg["url"]
        rows: list[dict] = []
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            log.error("rss %s failed: %s", name, e)
            return rows
        for e in parsed.entries[:limit]:
            published = _iso(getattr(e, "published_parsed", None)) or _iso(
                getattr(e, "published", "") or getattr(e, "updated", "")
            )
            if not _recent(published, window):
                continue
            link = getattr(e, "link", "")
            body = e.get("summary") or ""
            if getattr(e, "content", None):
                body = e.content[0].get("value", body)
            rows.append({
                "uid": _uid("rss", link or e.get("title", "")),
                "title": _clean(e.get("title"), 400),
                "url": link,
                "source": name,
                "published": published,
                "body": _clean(body),
                "extra": {"curated": False},
            })
        log.info("rss %s: %d items", name, len(rows))
        return rows

    feeds = cfg.get("feeds", [])
    with cf.ThreadPoolExecutor(min(8, max(1, len(feeds)))) as ex:
        for rows in ex.map(one, feeds):
            out.extend(rows)
    return out


def sitemap(cfg: dict, window: int) -> list[dict]:
    """For publishers with no RSS feed (e.g. Anthropic).

    Reads sitemap.xml, keeps URLs matching `path_contains` whose <lastmod>
    falls inside the window, then fetches each page for title/description.
    """
    out: list[dict] = []
    for site in cfg.get("sites", []):
        name, url = site["name"], site["url"]
        needle = site.get("path_contains", "")
        cap = site.get("max_pages", 12)
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:  # noqa: BLE001
            log.error("sitemap %s failed: %s", name, e)
            continue

        candidates: list[tuple[str, str]] = []
        for node in root.iter(f"{SM_NS}url"):
            loc = (node.findtext(f"{SM_NS}loc") or "").strip()
            lastmod = _iso(node.findtext(f"{SM_NS}lastmod") or "")
            if not loc or (needle and needle not in loc):
                continue
            if lastmod and not _recent(lastmod, window):
                continue
            candidates.append((loc, lastmod))

        # Newest first; undated sort last so dated posts win the budget.
        candidates.sort(key=lambda t: t[1] or "0000-00-00", reverse=True)
        candidates = candidates[:cap]

        def fetch(pair: tuple[str, str]) -> dict | None:
            loc, lastmod = pair
            try:
                pr = requests.get(loc, headers=UA, timeout=TIMEOUT)
                pr.raise_for_status()
                html = pr.text
            except Exception as e:  # noqa: BLE001
                log.warning("sitemap fetch %s: %s", loc, e)
                return None
            title = ""
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
            if m:
                title = m.group(1)
            if not title:
                m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
                title = m.group(1) if m else ""
            desc = ""
            for pat in (
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
            ):
                m = re.search(pat, html, re.I)
                if m:
                    desc = m.group(1)
                    break
            paragraphs = " ".join(re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)[:12])
            body = _clean(f"{desc} {paragraphs}")
            title = _clean(title, 400)
            if not title or len(body) < 80:
                return None
            return {
                "uid": _uid("sitemap", loc),
                "title": title,
                "url": loc,
                "source": name,
                "published": lastmod,
                "body": body,
                "extra": {"curated": False},
            }

        if candidates:
            with cf.ThreadPoolExecutor(6) as ex:
                for row in ex.map(fetch, candidates):
                    if row:
                        out.append(row)
        log.info("sitemap %s: %d items", name, len(out))
    return out


ADAPTERS = {"hf_daily": hf_daily, "arxiv": arxiv, "rss": rss, "sitemap": sitemap}


DATED = ("hf_daily", "arxiv")   # adapters that can query a specific day


def _finalise(items: list[dict], category: str) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        if it["uid"] in seen:
            continue
        seen.add(it["uid"])
        it["category"] = category
        unique.append(it)
    return unique


def prefetch_undated(sources: Iterable[dict], window: int) -> dict[int, dict[str, list[dict]]]:
    """Fetch feed/sitemap sources ONCE and bucket their items by publish date.

    Backfilling 30 days must not mean re-fetching every RSS feed 30 times:
    a feed returns the same payload regardless of the date we are building.
    Keyed by position in `sources` so two rss blocks never collide.
    """
    buckets: dict[int, dict[str, list[dict]]] = {}
    for idx, src in enumerate(sources):
        kind = src.get("type")
        if kind in DATED:
            continue
        fn = ADAPTERS.get(kind)
        if not fn:
            log.error("unknown source type %r", kind)
            continue
        try:
            items = fn(src, window)
        except Exception as e:  # noqa: BLE001
            log.exception("prefetch %s crashed: %s", kind, e)
            items = []
        by_date: dict[str, list[dict]] = {}
        for it in items:
            by_date.setdefault(it.get("published") or "", []).append(it)
        buckets[idx] = by_date
    return buckets


def collect_for_date(
    sources: Iterable[dict],
    window: int,
    category: str,
    date: str,
    prefetched: dict[int, dict[str, list[dict]]],
) -> list[dict]:
    """Assemble one historical day: live date-queries + pre-bucketed feeds."""
    items: list[dict] = []
    for idx, src in enumerate(sources):
        kind = src.get("type")
        fn = ADAPTERS.get(kind)
        if not fn:
            continue
        if kind in DATED:
            try:
                items.extend(fn(src, window, date=date))
            except Exception as e:  # noqa: BLE001
                log.exception("source %s/%s crashed on %s: %s", category, kind, date, e)
        else:
            items.extend(prefetched.get(idx, {}).get(date, []))
    return _finalise(items, category)


def collect(sources: Iterable[dict], window: int, category: str) -> list[dict]:
    """Run every configured source for one category and de-duplicate by uid."""
    items: list[dict] = []
    for src in sources:
        kind = src.get("type")
        fn = ADAPTERS.get(kind)
        if not fn:
            log.error("unknown source type %r (have: %s)", kind, ", ".join(ADAPTERS))
            continue
        try:
            items.extend(fn(src, window))
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            log.exception("source %s/%s crashed: %s", category, kind, e)

    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        if it["uid"] in seen:
            continue
        seen.add(it["uid"])
        it["category"] = category
        unique.append(it)
    log.info("category %s: %d unique items collected", category, len(unique))
    return unique
