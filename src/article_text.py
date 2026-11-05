from __future__ import annotations

import concurrent.futures as cf
import logging

import requests
import trafilatura

from feeds import Article
from utils import truncate

log = logging.getLogger("article_text")


def fetch_one(a: Article, settings: dict) -> Article:
    if not settings.get("fetch_article_text", True):
        return a
    # Do not attempt to bypass paywalls/login. Only fetch publicly returned HTML.
    try:
        resp = requests.get(
            a.link,
            timeout=settings.get("request_timeout", 20),
            headers={"User-Agent": settings.get("user_agent", "Mozilla/5.0")},
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return a
        if not a.image:
            try:
                from images import extract_page_image
                a.image = extract_page_image(resp.text, a.link)
            except Exception:
                pass
        from access import detect_paywall
        paywalled, _reason = detect_paywall(resp.text)
        if paywalled is True:
            return a
        extracted = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted and len(extracted) > 180:
            a.text = truncate(extracted, int(settings.get("article_text_chars", 2200)))
    except Exception as e:
        log.debug("Text fetch failed for %s: %s", a.link[:80], e)
    return a


def fetch_texts(articles: list[Article], settings: dict) -> list[Article]:
    with cf.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda a: fetch_one(a, settings), articles))
    return articles
