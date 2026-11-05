from __future__ import annotations

"""Verified free long reads from reputable international and specialist outlets.

This module does NOT write articles. It finds real published pieces, fetches the
HTML the publisher returns publicly, checks that the piece is not behind a
subscription/registration wall, counts the actual article words, and only then
offers it as a long read. Short wire reports are not used to fill quotas.
"""

import concurrent.futures as cf
import logging
import math
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

import feedparser
import requests
from dateutil import parser as dtparser

from access import extract_public_article
from feeds import Article, canonical_url, decode_google_links, gnews_url
from ranking import GENERIC_CONCEPTS, concept_set
from utils import clean_html, truncate

log = logging.getLogger("further_reading")
logging.getLogger("httpx").setLevel(logging.WARNING)

# name = how the reader should see the publisher; priority = editorial preference.
# access "free" means the outlet is normally free; "conditional" means we must
# verify each individual article because the outlet mixes free and paid pieces.
PUBLISHERS: dict[str, dict] = {
    "theguardian.com": {"name": "The Guardian", "priority": 10, "access": "free", "lang": "en"},
    "bbc.com": {"name": "BBC", "priority": 9, "access": "free", "lang": "en"},
    "bbc.co.uk": {"name": "BBC", "priority": 9, "access": "free", "lang": "en"},
    "aljazeera.com": {"name": "Al Jazeera English", "priority": 9, "access": "free", "lang": "en"},
    "reuters.com": {"name": "Reuters", "priority": 8, "access": "free", "lang": "en"},
    "apnews.com": {"name": "Associated Press", "priority": 8, "access": "free", "lang": "en"},
    "nytimes.com": {"name": "The New York Times", "priority": 8, "access": "conditional", "lang": "en"},
    "lemonde.fr": {"name": "Le Monde", "priority": 7, "access": "conditional", "lang": "en"},
    "foreignpolicy.com": {"name": "Foreign Policy", "priority": 7, "access": "conditional", "lang": "en"},
    "bloomberg.com": {"name": "Bloomberg", "priority": 8, "access": "conditional", "lang": "en"},
    "cnbc.com": {"name": "CNBC", "priority": 6, "access": "free", "lang": "en"},
    "independent.co.uk": {"name": "The Independent", "priority": 6, "access": "conditional", "lang": "en"},
    "jpost.com": {"name": "The Jerusalem Post", "priority": 6, "access": "conditional", "lang": "en"},
    "crisisgroup.org": {"name": "International Crisis Group", "priority": 8, "access": "free", "lang": "en"},
    "carnegieendowment.org": {"name": "Carnegie Endowment", "priority": 7, "access": "free", "lang": "en"},
    "carnegie-mec.org": {"name": "Carnegie Middle East", "priority": 7, "access": "free", "lang": "en"},
    "chathamhouse.org": {"name": "Chatham House", "priority": 7, "access": "free", "lang": "en"},
    "ecfr.eu": {"name": "ECFR", "priority": 7, "access": "free", "lang": "en"},
    "brookings.edu": {"name": "Brookings", "priority": 7, "access": "free", "lang": "en"},
    "mei.edu": {"name": "Middle East Institute", "priority": 7, "access": "free", "lang": "en"},
    "atlanticcouncil.org": {"name": "Atlantic Council", "priority": 6, "access": "free", "lang": "en"},
    "theconversation.com": {"name": "The Conversation", "priority": 6, "access": "free", "lang": "en"},
    "justsecurity.org": {"name": "Just Security", "priority": 6, "access": "free", "lang": "en"},
    "lawfaremedia.org": {"name": "Lawfare", "priority": 6, "access": "free", "lang": "en"},
    "syriadirect.org": {"name": "Syria Direct", "priority": 6, "access": "free", "lang": "en"},
    "enabbaladi.net": {"name": "عنب بلدي", "priority": 6, "access": "free", "lang": "ar"},
    "aljazeera.net": {"name": "الجزيرة", "priority": 7, "access": "free", "lang": "ar"},
    "bbc.com/arabic": {"name": "BBC عربي", "priority": 7, "access": "free", "lang": "ar"},
    "noonpost.com": {"name": "نون بوست", "priority": 5, "access": "free", "lang": "ar"},
    "middleeasteye.net": {"name": "Middle East Eye", "priority": 6, "access": "free", "lang": "en"},
    "newarab.com": {"name": "The New Arab", "priority": 5, "access": "free", "lang": "en"},
}

# Hard-paid outlets are never offered as free reads.
HARD_PAYWALL = {
    "ft.com", "wsj.com", "economist.com", "foreignaffairs.com",
    "al-monitor.com", "theinformation.com", "semafor.com", "businessinsider.com",
}
NOT_ARTICLES = {
    "facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "youtu.be", "tiktok.com", "linkedin.com", "reddit.com", "telegram.me", "t.me",
    "whatsapp.com", "pinterest.com", "wikipedia.org", "msn.com", "yahoo.com",
    "ground.news", "flipboard.com",
}
BAD_URL_PARTS = (
    "/video/", "/videos/", "/live/", "/liveblog/", "/gallery/", "/galleries/",
    "/pictures/", "/podcast/", "/podcasts/", "/audio/", "/shorts/", "/reels/",
    "/crossword/", "/quiz/", "/live-updates/", "/hub/", "/topics/", "/topic/",
    "/tag/", "/author/", "/section/",
)

REGION_WORDS = {
    "syria", "syrian", "damascus", "aleppo", "idlib", "homs", "raqqa", "hasakah",
    "lebanon", "beirut", "hezbollah", "israel", "israeli", "gaza", "palestine",
    "palestinian", "iran", "tehran", "iraq", "baghdad", "turkey", "ankara",
    "jordan", "egypt", "gulf", "saudi", "riyadh", "uae", "qatar", "yemen",
    "houthis", "middle east", "levant", "سوريا", "سورية", "دمشق", "حلب", "إدلب",
    "لبنان", "بيروت", "حزب الله", "إسرائيل", "غزة", "فلسطين", "إيران", "طهران",
    "العراق", "بغداد", "تركيا", "أنقرة", "الأردن", "مصر", "الخليج", "السعودية",
    "الرياض", "الإمارات", "قطر", "اليمن", "الحوثيون", "الشرق الأوسط",
}
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "what", "how", "why",
    "after", "before", "amid", "over", "into", "onto", "says", "said", "will",
    "could", "would", "should", "have", "has", "had", "been", "were", "was",
    "are", "but", "not", "you", "your", "its", "his", "her", "their", "than",
    "then", "them", "they", "about", "more", "most", "some", "such", "only",
    "في", "من", "على", "إلى", "عن", "مع", "هذا", "هذه", "ذلك", "التي", "الذي",
    "ما", "لا", "أن", "إن", "قد", "كان", "كانت", "بين", "بعد", "قبل", "حتى",
}
COMMON_NEWS = {
    "says", "said", "say", "reports", "report", "new", "first", "second", "third",
    "last", "moves", "closer", "amid", "after", "before", "over", "under", "into",
    "gets", "makes", "takes", "gives", "shows", "tells", "calls", "seeks", "plans",
    "announces", "announced", "welcomes", "condemns", "urges", "warns", "reveals",
}
DEEP_HINTS = (
    "analysis", "explainer", "in-depth", "long read", "longread", "background",
    "investigation", "profile", "explained", "what next", "the case", "inside",
    "تحليل", "خلفية", "تفاصيل", "قراءة", "تحقيق", "ملف",
)
RELEVANCE_GENERIC_CONCEPTS = GENERIC_CONCEPTS | {
    "agreement", "defence", "investment", "loan", "visit", "meeting", "economic",
    "killed", "blast", "arrest", "plot", "deny", "hezbollah_denial",
}
PLACE_CONCEPTS = {
    "saudi", "riyadh", "iran", "tehran", "israel", "gaza", "iraq", "baghdad",
    "turkey", "ankara", "beirut", "lebanon", "homs", "aleppo", "idlib", "damascus",
    "egypt", "cairo", "jordan", "amman", "qatar", "doha", "uae", "dubai",
}
LONG_URL_HINTS = (
    "/features/", "/longform/", "/long-read/", "/longread/", "/analysis/",
    "/investigations/", "/investigation/", "/insight/", "/special-report/",
    "/news/series/the-long-read/", "/opinion/", "/commentisfree/",
)

_VERIFY_CACHE: dict[str, dict | None] = {}


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def publisher_for(url: str) -> tuple[str, dict] | tuple[None, None]:
    h = _host(url)
    path = urlparse(url).path.lower()
    # More specific keys first (e.g. bbc.com/arabic).
    for key in sorted(PUBLISHERS, key=len, reverse=True):
        if "/" in key:
            domain, prefix = key.split("/", 1)
            if (h == domain or h.endswith("." + domain)) and path.startswith("/" + prefix):
                return key, PUBLISHERS[key]
        elif h == key or h.endswith("." + key):
            return key, PUBLISHERS[key]
    return None, None


def _blocked(url: str) -> bool:
    h = _host(url)
    for bad in HARD_PAYWALL | NOT_ARTICLES:
        if h == bad or h.endswith("." + bad):
            return True
    return False


def _is_article_url(url: str) -> bool:
    p = urlparse(url).path.lower()
    return not any(part in p for part in BAD_URL_PARTS)


def _words(text: str) -> list[str]:
    raw = [w for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", text or "").split()
           if w.lower() not in STOP and w.lower() not in COMMON_NEWS and len(w) > 2]

    def rank(w: str):
        proper = 1 if (w[:1].isupper() and not w.isupper()) or w.isupper() else 0
        return -(proper * 3 + min(len(w), 12))

    return sorted(raw, key=rank)


def _stem(w: str) -> str:
    w = w.lower().replace("’s", "").replace("'s", "")
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _token_set(text: str) -> set[str]:
    return {_stem(w) for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", text or "").split()
            if len(w) > 2 and w.lower() not in STOP}


def _regional(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in REGION_WORDS)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = dtparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _query_gnews(query: str, settings: dict, hl="en-US", gl="US", ceid="US:en") -> list[dict]:
    try:
        resp = requests.get(
            gnews_url({"query": query, "hl": hl, "gl": gl, "ceid": ceid}),
            timeout=settings.get("request_timeout", 20),
            headers={"User-Agent": settings.get("user_agent", "Mozilla/5.0")},
        )
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.debug("Long-read query failed (%s): %s", query[:60], e)
        return []
    out = []
    for e in parsed.entries[:30]:
        link = e.get("link", "")
        title = clean_html(e.get("title", ""))
        if not link or not title:
            continue
        pub = (e.get("source") or {}).get("title", "")
        if pub and title.endswith(f" - {pub}"):
            title = title[: -len(pub) - 3].strip()
        out.append({
            "title": title,
            "source_hint": pub,
            "link": link,
            "published": e.get("published", ""),
            "snippet": truncate(clean_html(e.get("summary", "")), 300),
            "is_google": "news.google.com" in link,
        })
    return out


def _queries(items: list[Article], settings: dict) -> list[tuple[str, str, str, str]]:
    days = int(settings.get("long_read_lookback_days", 90))
    region_en = ('(Syria OR "Middle East" OR Lebanon OR Palestine OR Israel OR Iran '
                 'OR Iraq OR Turkey OR Jordan OR Egypt OR Gulf OR Yemen)')
    region_ar = ('(سوريا OR سورية OR لبنان OR فلسطين OR إسرائيل OR إيران OR العراق '
                 'OR تركيا OR الأردن OR مصر OR الخليج OR اليمن)')
    q: list[tuple[str, str, str, str]] = [
        (f'site:theguardian.com {region_en} (analysis OR "long read" OR investigation OR profile OR explainer) when:{days}d', "en-US", "US", "US:en"),
        (f'site:bbc.com/news {region_en} when:{days}d', "en-US", "US", "US:en"),
        (f'site:aljazeera.com/features/ {region_en} when:{days}d', "en-US", "US", "US:en"),
        (f'site:aljazeera.com/features/longform/ {region_en} when:180d', "en-US", "US", "US:en"),
        (f'site:theguardian.com/news/series/the-long-read {region_en} when:180d', "en-US", "US", "US:en"),
        (f'site:reuters.com/investigations/ {region_en} when:180d', "en-US", "US", "US:en"),
        (f'site:reuters.com {region_en} (investigation OR insight OR analysis OR "special report") when:{days}d', "en-US", "US", "US:en"),
        (f'site:apnews.com {region_en} (analysis OR investigation OR "in-depth") when:{days}d', "en-US", "US", "US:en"),
        (f'site:nytimes.com {region_en} when:30d', "en-US", "US", "US:en"),
        (f'site:lemonde.fr/en/ {region_en} when:{days}d', "en-US", "US", "US:en"),
        (f'site:foreignpolicy.com {region_en} when:{days}d', "en-US", "US", "US:en"),
        (f'site:bloomberg.com {region_en} when:30d', "en-US", "US", "US:en"),
        (f'site:cnbc.com {region_en} (analysis OR "in depth" OR explainer OR investigation OR energy) when:{days}d', "en-US", "US", "US:en"),
        (f'site:independent.co.uk {region_en} (analysis OR comment OR "long read" OR investigation OR explainer) when:{days}d', "en-US", "US", "US:en"),
        (f'site:jpost.com {region_en} (analysis OR "in depth" OR investigation OR explainer) when:{days}d', "en-US", "US", "US:en"),
        (f'(site:crisisgroup.org OR site:carnegieendowment.org OR site:chathamhouse.org OR site:ecfr.eu OR site:brookings.edu OR site:mei.edu) {region_en} when:180d', "en-US", "US", "US:en"),
        (f'(site:theconversation.com OR site:justsecurity.org OR site:lawfaremedia.org OR site:atlanticcouncil.org OR site:syriadirect.org OR site:middleeasteye.net) {region_en} when:{days}d', "en-US", "US", "US:en"),
        (f'site:aljazeera.net {region_ar} (تحليل OR تقرير OR تحقيق) when:{days}d', "ar", "AE", "AE:ar"),
        (f'(site:enabbaladi.net OR site:bbc.com/arabic OR site:noonpost.com) {region_ar} when:{days}d', "ar", "AE", "AE:ar"),
    ]
    # Story-specific passes, so a narrow topic is not lost inside broad queries.
    for a in items:
        words = _words(a.title)
        if not words:
            continue
        ar = [w for w in words if re.search(r"[\u0600-\u06FF]", w)]
        if ar:
            q.append((f"{' '.join(ar[:4])} (تحليل OR تحقيق OR تقرير موسع) when:{days}d", "ar", "AE", "AE:ar"))
        else:
            q.append((f"{' '.join(words[:4])} (analysis OR investigation OR explainer OR \"long read\" OR profile) when:{days}d", "en-US", "US", "US:en"))
    return q


def _topic_tokens(article: Article) -> set[str]:
    # Region names alone are not a topic: "Syria" or "Iran" must not make an
    # unrelated article look relevant to every story in the brief.
    return _token_set(article.title) - REGION_WORDS - COMMON_NEWS


def _match_score(article: Article, title: str, text: str = "") -> int:
    if not _regional(title + " " + text[:400]):
        return 0
    key = _topic_tokens(article)
    # Title-to-title overlap is much safer than matching against the whole
    # article body, where generic words create false topic matches.
    overlap = len(key & _token_set(title))
    concepts = ((concept_set(article.title) & concept_set(title + " " + text[:500]))
                - RELEVANCE_GENERIC_CONCEPTS - PLACE_CONCEPTS)
    if overlap < 2 and not concepts:
        return 0
    return overlap * 3 + len(concepts) * 5


def _collect_candidates(items: list[Article], settings: dict) -> list[dict]:
    queries = _queries(items, settings)
    raw: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_query_gnews, q, settings, hl, gl, ceid)
                   for q, hl, gl, ceid in queries]
        for fut in cf.as_completed(futures):
            try:
                raw.extend(fut.result())
            except Exception as e:
                log.debug("query worker failed: %s", e)

    exclude = {canonical_url(a.link) for a in items}
    seen: set[str] = set()
    candidates: list[dict] = []
    for c in raw:
        curl = canonical_url(c["link"])
        if curl in seen or curl in exclude:
            continue
        if _blocked(c["link"]):
            continue
        title_low = c["title"].lower()
        if not _regional(c["title"] + " " + c["snippet"]):
            continue
        # Pre-score before decoding; final relevance is recomputed after fetch.
        best = max((_match_score(a, c["title"], c["snippet"]) for a in items), default=0)
        deep = 4 if any(h in title_low for h in DEEP_HINTS) else 0
        url_hint = 3 if any(h in c["link"].lower() for h in LONG_URL_HINTS) else 0
        pub_hint = 0
        key, pub = publisher_for(c["link"])
        if pub:
            pub_hint = pub["priority"]
        else:
            hint = c.get("source_hint", "").lower()
            for p in PUBLISHERS.values():
                if p["name"].lower() in hint:
                    pub_hint = p["priority"]
                    break
        dt = _parse_date(c.get("published"))
        recency = 0
        if dt:
            age = max(0, (datetime.now(timezone.utc) - dt).days)
            recency = max(0, 4 - age // 15)
        sim = max((SequenceMatcher(None, a.title.lower(), title_low).ratio() for a in items), default=0)
        if sim > 0.86:
            continue
        c["_pre_score"] = best + deep + url_hint + pub_hint + recency
        c["_sim"] = sim
        seen.add(curl)
        candidates.append(c)

    candidates.sort(key=lambda x: x["_pre_score"], reverse=True)
    limit = int(settings.get("long_read_max_candidates", 90))
    candidates = candidates[:limit]

    google_ones = [c for c in candidates if c["is_google"]]
    if google_ones and settings.get("decode_google_links", True):
        holders = [
            Article(source=c.get("source_hint", ""), source_lang="en", source_category="politics",
                    source_weight=1, title=c["title"], link=c["link"], published=None,
                    snippet="", is_google=True)
            for c in google_ones
        ]
        try:
            decode_google_links(holders)
            for c, h in zip(google_ones, holders):
                c["link"] = h.link
                c["is_google"] = "news.google.com" in h.link
        except Exception as e:
            log.debug("Decode failed for long reads: %s", e)

    out: list[dict] = []
    used: set[str] = set()
    for c in candidates:
        if c["is_google"] or "news.google.com" in c["link"]:
            continue
        curl = canonical_url(c["link"])
        if curl in used or curl in exclude or _blocked(c["link"]):
            continue
        key, pub = publisher_for(c["link"])
        if not pub or not _is_article_url(c["link"]):
            continue
        used.add(curl)
        c["publisher_key"] = key
        c["publisher"] = pub
        out.append(c)

    # Keep a per-publisher quota so one prolific outlet does not crowd out
    # Guardian / AJE / Reuters / AP / NYT / Le Monde / FP before verification.
    by_pub: dict[str, list[dict]] = {}
    for c in out:
        by_pub.setdefault(c["publisher_key"], []).append(c)
    per_pub = int(settings.get("long_read_candidates_per_publisher", 6))
    chosen: list[dict] = []
    chosen_urls: set[str] = set()
    for key, lst in by_pub.items():
        for c in sorted(lst, key=lambda x: x["_pre_score"], reverse=True)[:per_pub]:
            chosen.append(c)
            chosen_urls.add(canonical_url(c["link"]))
    for c in sorted(out, key=lambda x: x["_pre_score"], reverse=True):
        if len(chosen) >= int(settings.get("long_read_verify_limit", 90)):
            break
        if canonical_url(c["link"]) not in chosen_urls:
            chosen.append(c)
            chosen_urls.add(canonical_url(c["link"]))
    chosen.sort(key=lambda x: x["_pre_score"], reverse=True)
    return chosen


def _verify_one(c: dict, settings: dict) -> dict | None:
    url = canonical_url(c["link"])
    if url in _VERIFY_CACHE:
        cached = _VERIFY_CACHE[url]
        return dict(cached, **{"title": c["title"]}) if cached else None
    result: dict | None = None
    try:
        resp = requests.get(
            c["link"],
            timeout=int(settings.get("long_read_fetch_timeout", 12)),
            headers={"User-Agent": settings.get("user_agent", "Mozilla/5.0")},
            allow_redirects=True,
        )
        if resp.status_code in (401, 403, 407, 429, 451) or resp.status_code >= 500:
            _VERIFY_CACHE[url] = None
            return None
        if resp.status_code >= 400:
            _VERIFY_CACHE[url] = None
            return None
        html = resp.text[: int(settings.get("long_read_max_html_bytes", 2_500_000))]
        info = extract_public_article(html)
        if info["paywalled"] is True:
            _VERIFY_CACHE[url] = None
            return None
        lang = info["lang"] if info["lang"] in ("ar", "en") else c["publisher"].get("lang", "en")
        min_words = int(settings.get("long_read_min_words_ar", 600) if lang == "ar"
                        else settings.get("long_read_min_words_en", 800))
        if info["words"] < min_words or info["minutes"] < int(settings.get("long_read_min_minutes", 4)):
            _VERIFY_CACHE[url] = None
            return None
        # A JS shell / consent page is not a verified article.
        if info["access_reason"].startswith("shell:") or info["words"] < min_words:
            _VERIFY_CACHE[url] = None
            return None
        title = c["title"] or info["title"]
        dt = _parse_date(info["date"]) or _parse_date(c.get("published"))
        result = {
            "title": title,
            "source": c["publisher"]["name"],
            "publisher_key": c["publisher_key"],
            "link": c["link"],
            "lang": lang,
            "words": info["words"],
            "minutes": info["minutes"],
            "date": dt.strftime("%Y-%m-%d") if dt else "",
            "published_dt": dt.isoformat() if dt else "",
            "label": _label(c["link"], title, c["publisher_key"]),
            "priority": c["publisher"]["priority"],
            "text_sample": info["text"][:1500],
            "access_reason": info["access_reason"],
        }
    except Exception as e:
        log.debug("Long-read verify failed %s: %s", c["link"][:80], e)
        result = None
    _VERIFY_CACHE[url] = result
    return result


def _label(url: str, title: str, key: str) -> str:
    low = (url + " " + title).lower()
    if key in {"crisisgroup.org", "carnegieendowment.org", "carnegie-mec.org", "chathamhouse.org",
               "ecfr.eu", "brookings.edu", "mei.edu", "atlanticcouncil.org", "theconversation.com"}:
        return "ورقة بحثية / Research"
    if any(h in low for h in ("investigation", "investigations", "تحقيق")):
        return "تحقيق موسّع / Investigation"
    if any(h in low for h in ("analysis", "explainer", "explained", "تحليل", "قراءة")):
        return "تحليل موسّع / Analysis"
    if any(h in low for h in ("opinion", "commentisfree", "رأي")):
        return "رأي موسّع / Opinion"
    return "مقال موسّع / Long read"


def _public_read(v: dict, scope: str = "topic") -> dict:
    return {
        "title": v["title"],
        "source": v["source"],
        "link": v["link"],
        "lang": v["lang"],
        "label": v["label"],
        "words": v["words"],
        "minutes": v["minutes"],
        "date": v["date"],
        "scope": scope,
    }


def attach(items_articles: list[Article], settings: dict, per_item: int = 5) -> list[dict]:
    """Populate verified long reads per story; return a separate global list."""
    if not settings.get("further_reading_enabled", True) or not items_articles:
        return []
    _VERIFY_CACHE.clear()
    candidates = _collect_candidates(items_articles, settings)
    log.info("Long-read candidates after publisher/URL filters: %d", len(candidates))
    if not candidates:
        return []

    verify_limit = int(settings.get("long_read_verify_limit", 70))
    candidates = candidates[:verify_limit]
    verified: list[dict] = []
    workers = int(settings.get("long_read_workers", 6))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_verify_one, c, settings) for c in candidates]
        for fut in cf.as_completed(futures):
            try:
                v = fut.result()
                if v:
                    verified.append(v)
            except Exception as e:
                log.debug("verify worker failed: %s", e)

    # Deduplicate verified pieces by real URL and near-identical title.
    unique: list[dict] = []
    used_urls: set[str] = set()
    used_titles: set[str] = set()
    for v in sorted(verified, key=lambda x: (x["priority"], x["words"]), reverse=True):
        curl = canonical_url(v["link"])
        tkey = re.sub(r"\W+", "", v["title"].lower())[:70]
        if curl in used_urls or tkey in used_titles:
            continue
        used_urls.add(curl)
        used_titles.add(tkey)
        v["matches"] = {a.id: _match_score(a, v["title"], v["text_sample"]) for a in items_articles}
        unique.append(v)
    log.info("Verified free long reads: %d", len(unique))

    used: set[str] = set()
    # Reserve a diverse global section first so the international papers are
    # visible even when a narrow daily story has no matching long read.
    global_target = int(settings.get("global_long_reads", 6))
    global_reads: list[dict] = []
    global_pubs: set[str] = set()
    globals_pool = sorted(unique, key=lambda v: (v["priority"], v["date"], v["words"]), reverse=True)
    for v in globals_pool:
        if v["source"] in global_pubs:
            continue
        global_reads.append(_public_read(v, "global"))
        global_pubs.add(v["source"])
        used.add(canonical_url(v["link"]))
        if len(global_reads) >= global_target:
            break

    # Then attach only genuinely related long reads under each story.
    threshold = int(settings.get("long_read_match_threshold", 8))
    for a in items_articles:
        pool_items = [v for v in unique
                      if canonical_url(v["link"]) not in used
                      and v["matches"].get(a.id, 0) >= threshold]
        pool_items.sort(key=lambda v: (v["matches"].get(a.id, 0), v["priority"], v["words"]), reverse=True)
        chosen: list[dict] = []
        pubs: set[str] = set()
        for v in pool_items:
            if v["source"] in pubs:
                continue
            chosen.append(_public_read(v, "topic"))
            pubs.add(v["source"])
            used.add(canonical_url(v["link"]))
            if len(chosen) >= per_item:
                break
        a.further_reading = chosen

    total = sum(len(a.further_reading) for a in items_articles)
    log.info("Long reads attached: %d under stories + %d global", total, len(global_reads))
    return global_reads


# Backwards-compatible helper for manual tests / older callers.
def find_for_article(article: Article, settings: dict, count: int = 5,
                     exclude_urls: set[str] | None = None) -> list[dict]:
    article.further_reading = []
    attach([article], settings, count)
    return article.further_reading
