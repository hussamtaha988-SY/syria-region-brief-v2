from __future__ import annotations

"""Further reading: more FREE, in-depth articles about the same story.

This is not an AI-written report. It is a curated reading list of real articles
the reader can open: analysis, background and explainers from outlets and
think tanks that do not put their work behind a paywall.
"""

import concurrent.futures as cf
import logging
import re
from urllib.parse import urlparse

import feedparser
import requests

from feeds import Article, canonical_url, decode_google_links, gnews_url
from utils import clean_html, truncate

log = logging.getLogger("further_reading")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Sites whose long-form work is free to read. Used to bias toward depth.
ANALYSIS_DOMAINS = {
    "crisisgroup.org": "International Crisis Group",
    "carnegieendowment.org": "Carnegie",
    "carnegie-mec.org": "Carnegie Middle East",
    "chathamhouse.org": "Chatham House",
    "ecfr.eu": "ECFR",
    "brookings.edu": "Brookings",
    "atlanticcouncil.org": "Atlantic Council",
    "theconversation.com": "The Conversation",
    "bellingcat.com": "Bellingcat",
    "justsecurity.org": "Just Security",
    "lawfaremedia.org": "Lawfare",
    "mekansm.org": "Middle East Institute",
    "mei.edu": "Middle East Institute",
    "arabcenterdc.org": "Arab Center DC",
    "cfr.org": "Council on Foreign Relations",
    "usip.org": "USIP",
    "iiss.org": "IISS",
    "warontherocks.com": "War on the Rocks",
    "responsiblestatecraft.org": "Responsible Statecraft",
    "syriadirect.org": "Syria Direct",
    "enabbaladi.net": "عنب بلدي",
    "inkfada.com": "إنكفادا",
    "alaraby.co.uk": "العربي الجديد",
    "aljazeera.net": "الجزيرة",
    "aljazeera.com": "Al Jazeera",
    "middleeasteye.net": "Middle East Eye",
    "newarab.com": "The New Arab",
    "theguardian.com": "The Guardian",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
}

# Hard paywalls: never offer these as "free further reading".
PAYWALLED_DOMAINS = {
    "ft.com", "wsj.com", "nytimes.com", "bloomberg.com", "economist.com",
    "washingtonpost.com", "theatlantic.com", "newyorker.com", "foreignaffairs.com",
    "foreignpolicy.com", "telegraph.co.uk", "thetimes.co.uk", "thetimes.com",
    "lemonde.fr", "lefigaro.fr", "haaretz.com", "timesofisrael.com",
    "al-monitor.com", "middleeasteye.net.premium", "nikkei.com", "barrons.com",
    "businessinsider.com", "seekingalpha.com", "statista.com", "reutersconnect.com",
}

REGION_WORDS = {
    "syria", "syrian", "idlib", "aleppo", "homs", "damascus", "hasakah", "raqqa",
    "deir", "latakia", "tartus", "daraa", "sweida", "kurdish", "sdf", "hts",
    "lebanon", "lebanese", "beirut", "hezbollah", "iraq", "iraqi", "baghdad",
    "iran", "iranian", "tehran", "turkey", "turkish", "ankara", "erdogan",
    "israel", "israeli", "gaza", "palestin", "jordan", "amman", "egypt", "cairo",
    "saudi", "riyadh", "gulf", "qatar", "emirates", "kuwait", "bahrain", "oman",
    "yemen", "houthi", "middle", "east", "levant", "assad", "sharaa",
    "سوريا", "سورية", "سوري", "إدلب", "ادلب", "حلب", "حمص", "دمشق", "الحسكة",
    "الرقة", "دير", "اللاذقية", "درعا", "السويداء", "قسد", "الشرع",
    "لبنان", "لبناني", "بيروت", "حزب", "الله", "العراق", "عراقي", "بغداد",
    "إيران", "ايران", "إيراني", "طهران", "تركيا", "تركي", "أنقرة", "أردوغان",
    "إسرائيل", "اسرائيل", "غزة", "فلسطين", "الأردن", "عمان", "مصر", "القاهرة",
    "السعودية", "الرياض", "الخليج", "قطر", "الإمارات", "الكويت", "البحرين",
    "اليمن", "الحوثي", "الشرق", "الأوسط",
}

STOP = {
    "the", "a", "an", "in", "on", "at", "of", "and", "to", "for", "with", "after",
    "over", "says", "said", "its", "his", "her", "their", "from", "by", "as",
    "is", "are", "was", "were", "new", "will", "has", "have", "amid", "than",
    "في", "من", "على", "عن", "الى", "إلى", "مع", "بعد", "وقد", "التي", "الذي",
    "هذا", "هذه", "بين", "خلال", "أن", "إن", "ما", "لا", "قد", "كل",
}

OPINION_HINTS = ("opinion", "analysis", "explainer", "why", "how", "what next",
                 "رأي", "تحليل", "قراءة", "لماذا", "كيف", "مقال", "زاوية")
DEEP_HINTS = ("analysis", "explainer", "in-depth", "long read", "background",
              "timeline", "what we know", "تحليل", "خلفية", "تفاصيل", "قراءة")


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _base_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# Social posts, aggregators and mirrors are not articles.
NOT_ARTICLES = {
    "facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "youtu.be", "tiktok.com", "linkedin.com", "reddit.com",
    "telegram.me", "t.me", "whatsapp.com", "pinterest.com", "wikipedia.org",
    "msn.com", "news.yahoo.com", "yahoo.com", "ground.news", "flipboard.com",
}


def is_free(url: str) -> bool:
    host = _base_domain(_domain(url))
    return host not in PAYWALLED_DOMAINS and host not in NOT_ARTICLES


COMMON_NEWS = {
    "says","said","say","reports","report","new","first","second","third","last",
    "moves","closer","amid","after","before","over","under","into","onto","gets",
    "makes","takes","gives","shows","tells","calls","seeks","plans","could","would",
    "may","might","will","must","should","than","then","them","they","this","that",
    "these","those","here","there","what","when","where","which","while","about",
    "announces","announced","welcomes","condemns","urges","warns","reveals",
}


def _words(text: str) -> list[str]:
    """Distinctive keywords first: proper nouns and long, specific terms."""
    raw = [w for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", text or "").split()
           if w.lower() not in STOP and w.lower() not in COMMON_NEWS and len(w) > 2]
    def rank(w: str):
        proper = 1 if (w[:1].isupper() and not w.isupper()) or w.isupper() else 0
        return (-(proper * 3 + min(len(w), 12)), )
    # Stable sort keeps original order among equally distinctive words.
    return sorted(raw, key=rank)


def _label(url: str, title: str) -> str:
    host = _base_domain(_domain(url))
    t = (title or "").lower()
    if any(h in t for h in ("opinion", "رأي", "زاوية")):
        return "رأي / Opinion"
    if host in {_base_domain(d) for d in ANALYSIS_DOMAINS} and any(h in t for h in DEEP_HINTS):
        return "تحليل معمّق / In-depth"
    if any(h in t for h in ("analysis", "تحليل", "explainer", "قراءة")):
        return "تحليل / Analysis"
    if host in {"crisisgroup.org", "carnegieendowment.org", "carnegie-mec.org",
                "chathamhouse.org", "ecfr.eu", "brookings.edu", "atlanticcouncil.org",
                "mei.edu", "cfr.org", "usip.org", "iiss.org", "theconversation.com"}:
        return "ورقة بحثية / Research"
    return "تقرير / Report"


def _query_gnews(query: str, settings: dict, hl="en-US", gl="US", ceid="US:en") -> list[dict]:
    try:
        resp = requests.get(
            gnews_url({"query": query, "hl": hl, "gl": gl, "ceid": ceid}),
            timeout=settings.get("request_timeout", 20),
            headers={"User-Agent": settings.get("user_agent", "Mozilla/5.0")},
        )
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.debug("Further-reading query failed (%s): %s", query[:50], e)
        return []
    out = []
    for e in parsed.entries[:40]:
        link = e.get("link", "")
        title = clean_html(e.get("title", ""))
        if not link or not title:
            continue
        pub = (e.get("source") or {}).get("title", "")
        if pub and title.endswith(f" - {pub}"):
            title = title[: -len(pub) - 3].strip()
        out.append({
            "title": title,
            "source": pub,
            "link": link,
            "published": e.get("published", ""),
            "snippet": truncate(clean_html(e.get("summary", "")), 300),
            "is_google": "news.google.com" in link,
        })
    return out


def find_for_article(article: Article, settings: dict, count: int = 3,
                     exclude_urls: set[str] | None = None) -> list[dict]:
    """Find up to `count` free, deeper reads about this one story."""
    words = _words(article.title)
    if not words:
        return []
    key = {w.lower() for w in words}
    exclude = {canonical_url(article.link)}
    exclude.update(exclude_urls or set())

    queries = []
    # 1) Depth first: analysis / research sites, wider time window.
    sites = " OR ".join(f"site:{d}" for d in (
        "crisisgroup.org", "carnegieendowment.org", "carnegie-mec.org",
        "chathamhouse.org", "ecfr.eu", "brookings.edu", "atlanticcouncil.org",
        "mei.edu", "theconversation.com", "middleeasteye.net", "newarab.com",
        "syriadirect.org", "enabbaladi.net", "aljazeera.com", "theguardian.com",
    ))
    topic = " ".join(words[:5])
    queries.append((f"({sites}) {topic} when:90d", "en-US", "US", "US:en", 6))
    queries.append((f"{topic} (analysis OR explainer OR background OR \"what next\") when:90d",
                    "en-US", "US", "US:en", 4))
    # 2) Broader recent coverage of the same event.
    queries.append((f"{' '.join(words[:7])} when:14d", "en-US", "US", "US:en", 2))
    queries.append((f"{' '.join(words[:4])} (report OR investigation OR profile OR explained) when:60d",
                    "en-US", "US", "US:en", 2))
    # 3) Arabic coverage.
    ar_words = [w for w in words if re.search(r"[\u0600-\u06FF]", w)]
    if ar_words:
        queries.append((f"{' '.join(ar_words[:6])} when:21d", "ar", "AE", "AE:ar", 2))
        queries.append((f"{' '.join(ar_words[:4])} (تحليل OR تقرير OR قراءة) when:60d",
                        "ar", "AE", "AE:ar", 3))
    else:
        queries.append((f"{' '.join(words[:5])} سوريا when:14d", "ar", "AE", "AE:ar", 1))

    candidates: list[dict] = []
    seen: set[str] = set()
    for q, hl, gl, ceid, bonus in queries:
        for c in _query_gnews(q, settings, hl, gl, ceid):
            curl = canonical_url(c["link"])
            if curl in seen or curl in exclude:
                continue
            if not is_free(c["link"]):
                continue
            t_words = {w.lower() for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", c["title"]).split()}
            overlap = len(key & t_words)
            regional = bool(t_words & REGION_WORDS)
            if overlap < 2 or not regional:
                continue
            seen.add(curl)
            host = _base_domain(_domain(c["link"]))
            depth = 6 if host in {_base_domain(d) for d in ANALYSIS_DOMAINS} else 0
            longish = 1 if len(c.get("snippet", "")) > 160 else 0
            t_low = c["title"].lower()
            analytic = 4 if any(h in t_low for h in DEEP_HINTS) else 0
            # Wire reprints of the same headline add no depth: push them down.
            from difflib import SequenceMatcher
            sim = SequenceMatcher(None, article.title.lower(), t_low).ratio()
            reprint = -8 if sim > 0.72 else 0
            c["_score"] = overlap * 2 + bonus + depth + longish + analytic + reprint
            c["_sim"] = sim
            candidates.append(c)

    if len(candidates) < count * 2:
        # Looser top-up passes: fewer keywords, still regional and free.
        for q, hl, gl, ceid in (
            (f"{' '.join(words[:3])} when:45d", "en-US", "US", "US:en"),
            (f"{' '.join(words[:2])} Syria when:60d", "en-US", "US", "US:en"),
            (f"{' '.join(words[:3])} سوريا when:45d", "ar", "AE", "AE:ar"),
            (f"{' '.join(words[:2])} سوريا تحليل when:90d", "ar", "AE", "AE:ar"),
        ):
            for c in _query_gnews(q, settings, hl, gl, ceid):
                curl = canonical_url(c["link"])
                if curl in seen or curl in exclude or not is_free(c["link"]):
                    continue
                t_words = {w.lower() for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", c["title"]).split()}
                if not (t_words & REGION_WORDS) or not (key & t_words):
                    continue
                seen.add(curl)
                c["_score"] = 1
                c["_sim"] = 0
                candidates.append(c)
                if len(candidates) >= count * 3:
                    break
            if len(candidates) >= count * 2:
                break

    if not candidates:
        return []

    # Resolve Google redirects so the reader sees the real publisher URL.
    candidates.sort(key=lambda x: x["_score"], reverse=True)
    candidates = candidates[: max(count * 4, 12)]
    google_ones = [c for c in candidates if c["is_google"]]
    if google_ones and settings.get("decode_google_links", True):
        holders = [
            Article(source=c["source"], source_lang="en", source_category="politics",
                    source_weight=1, title=c["title"], link=c["link"],
                    published=None, snippet="", is_google=True)
            for c in google_ones
        ]
        try:
            decode_google_links(holders)
            for c, h in zip(google_ones, holders):
                c["link"] = h.link
                c["is_google"] = False
        except Exception as e:
            log.debug("Decode failed for further reading: %s", e)

    # Re-filter after decoding, and drop duplicates by real URL and by title.
    final: list[dict] = []
    used_urls: set[str] = set()
    used_titles: set[str] = set()
    for c in sorted(candidates, key=lambda x: x["_score"], reverse=True):
        curl = canonical_url(c["link"])
        if curl in exclude or curl in used_urls:
            continue
        if not is_free(c["link"]):
            continue
        tkey = re.sub(r"\W+", "", c["title"].lower())[:60]
        if tkey in used_titles:
            continue
        if c.get("_sim", 0) > 0.80:
            continue
        host = _base_domain(_domain(c["link"]))
        if "news.google.com" in c["link"]:
            continue  # could not resolve; skip rather than send a redirect
        used_urls.add(curl)
        used_titles.add(tkey)
        final.append({
            "title": c["title"],
            "source": c["source"] or ANALYSIS_DOMAINS.get(host, host),
            "link": c["link"],
            "label": _label(c["link"], c["title"]),
            "lang": "ar" if re.search(r"[\u0600-\u06FF]", c["title"]) else "en",
        })
        if len(final) >= count:
            break
    return final


def attach(items_articles: list[Article], settings: dict, per_item: int = 3) -> None:
    """Populate article.further_reading for every selected article."""
    if not settings.get("further_reading_enabled", True):
        return
    exclude = {canonical_url(a.link) for a in items_articles}
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(find_for_article, a, settings, per_item, exclude): a
            for a in items_articles
        }
        for fut in cf.as_completed(futures):
            a = futures[fut]
            try:
                a.further_reading = fut.result()
            except Exception as e:
                log.debug("Further reading failed for %s: %s", a.id, e)
                a.further_reading = []
    total = sum(len(a.further_reading) for a in items_articles)
    log.info("Further reading: %d free in-depth links across %d items", total, len(items_articles))
