from __future__ import annotations

import concurrent.futures as cf
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse, urlunparse

import feedparser
import requests

from utils import clean_html, now_utc

log = logging.getLogger("feeds")


@dataclass
class Article:
    source: str
    source_lang: str
    source_category: str
    source_weight: int
    title: str
    link: str
    published: datetime | None
    snippet: str
    is_google: bool = False
    text: str = ""
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    category: str = "politics"
    id: str = ""

    def public_dict(self) -> dict:
        return {
            "source": self.source,
            "lang": self.source_lang,
            "title": self.title,
            "link": self.link,
            "published": self.published.isoformat() if self.published else None,
            "category": self.category,
            "tags": self.tags,
        }


def canonical_url(url: str) -> str:
    try:
        p = urlparse(url)
        query = "&".join(
            part for part in p.query.split("&")
            if part and not part.lower().startswith(("utm_", "oc=", "hl=", "gl=", "ceid="))
        )
        path = p.path.rstrip("/")
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))
    except Exception:
        return url


def gnews_url(src: dict) -> str:
    q = quote(src["query"], safe="")
    hl = src.get("hl", "en-US")
    gl = src.get("gl", "US")
    ceid = src.get("ceid", f"{gl}:en")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def parse_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_source(src: dict, settings: dict) -> list[Article]:
    name = src["name"]
    timeout = settings.get("request_timeout", 20)
    ua = settings.get("user_agent", "Mozilla/5.0")
    url = gnews_url(src) if src.get("type") == "gnews" else src["url"]
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("Feed failed: %s (%s)", name, e)
        return []

    cutoff = now_utc() - timedelta(hours=settings.get("hours_window", 30))
    articles: list[Article] = []
    for entry in parsed.entries[:80]:
        title = clean_html(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        # Google News titles usually end with " - Publisher Name".
        if src.get("type") == "gnews":
            publisher = ""
            try:
                publisher = entry.get("source", {}).get("title", "")
            except Exception:
                pass
            suffixes = [f" - {name}", f" - {name.split(' — ')[0]}"]
            if publisher:
                suffixes.append(f" - {publisher}")
            stripped = False
            for suffix in suffixes:
                if suffix and title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
                    stripped = True
                    break
            if not stripped:
                title = re.sub(r"\s+[-–|]\s+[^-–|]{2,45}$", "", title).strip()
        pub = parse_published(entry)
        if pub and pub < cutoff:
            continue
        snippet = clean_html(entry.get("summary", "") or entry.get("description", ""))
        articles.append(Article(
            source=name,
            source_lang=src.get("lang", "en"),
            source_category=src.get("category", "politics"),
            source_weight=int(src.get("weight", 3)),
            title=title,
            link=link,
            published=pub,
            snippet=snippet[:1200],
            is_google=(src.get("type") == "gnews"),
        ))
    log.info("Fetched %d items from %s", len(articles), name)
    return articles


def fetch_all(sources: list[dict], settings: dict) -> list[Article]:
    results: list[Article] = []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_source, src, settings) for src in sources]
        for fut in cf.as_completed(futures):
            try:
                results.extend(fut.result())
            except Exception as e:
                log.warning("Worker error: %s", e)
    return results


def decode_google_links(articles: list[Article], timeout: float = 12.0) -> None:
    """Resolve Google News redirect URLs to original publisher URLs, best effort."""
    targets = [a for a in articles if a.is_google and "news.google.com" in a.link]
    if not targets:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception as e:
        log.warning("googlenewsdecoder unavailable: %s", e)
        return
    try:
        results = gnewsdecoder([a.link for a in targets], interval=1, timeout=timeout)
        if isinstance(results, dict):
            results = [results]
        for art, result in zip(targets, results):
            if isinstance(result, dict):
                decoded = result.get("decoded_url")
                if (result.get("success") or result.get("status")) and decoded:
                    art.link = decoded
    except Exception as e:
        log.warning("Batch Google link decode failed: %s", e)
