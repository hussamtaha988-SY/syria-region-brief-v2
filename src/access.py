from __future__ import annotations

"""Public-access and article-length checks.

No paywall bypass: we only inspect HTML that the publisher returns publicly.
If a page asks for a subscription/registration, or we cannot verify the text,
it must not be presented as a free long read.
"""

import json
import math
import re
from html import unescape

import trafilatura

WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:['’\-][A-Za-zÀ-ÿ]+)*|[\u0600-\u06FF]+")

STRONG_WALL_PHRASES = (
    "subscribe to continue",
    "subscribe to read",
    "subscribe to unlock",
    "subscription required",
    "this article is for subscribers",
    "this content is for subscribers",
    "already a subscriber",
    "log in to continue reading",
    "login to continue reading",
    "sign in to continue reading",
    "register to continue reading",
    "create a free account to continue",
    "you've reached your article limit",
    "you have reached your article limit",
    "you've reached your monthly limit",
    "accès réservé aux abonnés",
    "article réservé aux abonnés",
    "vous devez être abonné",
    "abonnez-vous pour lire",
    "اشترك لمتابعة القراءة",
    "اشترك لقراءة المقال",
    "هذا المقال للمشتركين",
    "المحتوى للمشتركين",
    "سجل الدخول لمتابعة القراءة",
)

BOILERPLATE_HINTS = (
    "a required part of this site couldn’t load",
    "a required part of this site couldn't load",
    "enable javascript to view",
    "please enable javascript",
    "access denied",
    "are you a robot",
    "verify you are human",
)


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def detect_language(text: str, fallback: str = "en") -> str:
    if not text:
        return fallback
    ar = len(re.findall(r"[\u0600-\u06FF]", text))
    latin = len(re.findall(r"[A-Za-zÀ-ÿ]", text))
    if ar > latin * 0.6 and ar > 80:
        return "ar"
    if latin > 80:
        return "en"
    return fallback


def reading_minutes(words: int, lang: str = "en") -> int:
    rate = 170 if lang == "ar" else 200
    return max(1, math.ceil(words / rate))


def _jsonld_nodes(html: str):
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(unescape(raw.strip()))
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(node.get("@graph", []) or [])
                for key in ("mainEntity", "mainEntityOfPage", "hasPart"):
                    val = node.get(key)
                    if isinstance(val, (dict, list)):
                        stack.extend(val if isinstance(val, list) else [val])
            elif isinstance(node, list):
                stack.extend(node)


def detect_paywall(html: str) -> tuple[bool | None, str]:
    """Return (is_paywalled, reason). None means unknown/not determinable."""
    if not html:
        return None, "empty"
    low = html.lower()

    for node in _jsonld_nodes(html):
        val = node.get("isAccessibleForFree")
        if val is False or (isinstance(val, str) and val.lower() == "false"):
            return True, "metadata:isAccessibleForFree=false"
        if val is True or (isinstance(val, str) and val.lower() == "true"):
            # Metadata says free, but still check visible hard walls below.
            break

    for pattern in (
        r'<meta[^>]+(?:name|property)=["\'](?:content_tier|content-tier|article:content_tier)["\'][^>]+content=["\'](?:locked|paid|subscriber|premium)["\']',
        r'<meta[^>]+content=["\'](?:locked|paid|subscriber|premium)["\'][^>]+(?:name|property)=["\'](?:content_tier|content-tier|article:content_tier)["\']',
    ):
        if re.search(pattern, low, flags=re.I):
            return True, "metadata:content_tier"

    for phrase in STRONG_WALL_PHRASES:
        if phrase in low:
            return True, f"text:{phrase}"

    # Le Monde / other JS shells sometimes return 200 with no article.
    for hint in BOILERPLATE_HINTS:
        if hint in low:
            return None, f"shell:{hint}"

    return False, "no-wall-detected"


def extract_public_article(html: str) -> dict:
    """Extract article text and basic metadata from publicly returned HTML."""
    paywalled, reason = detect_paywall(html)
    text = ""
    if paywalled is not True:
        try:
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            ) or ""
        except Exception:
            text = ""
    words = count_words(text)
    lang = detect_language(text)
    title = ""
    date = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta:
            title = meta.title or ""
            date = meta.date or ""
    except Exception:
        pass
    return {
        "text": text,
        "words": words,
        "lang": lang,
        "title": title,
        "date": date,
        "paywalled": paywalled,
        "access_reason": reason,
        "minutes": reading_minutes(words, lang),
    }
