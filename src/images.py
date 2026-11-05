from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import requests

from feeds import Article

log = logging.getLogger("images")

# Tracking pixels / ad beacons that are not article artwork.
BAD_IMAGE_HINTS = (
    "doubleclick", "googlesyndication", "google-analytics", "analytics",
    "pixel", "spacer", "blank.gif", "1x1", "avatar", "logo", "sprite",
    "emoji", "feedburner", "gravatar", "badge", "icon",
)
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".avif")

OG_PATTERNS = (
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
    r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)',
)


def plausible_image(url: str) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    if u.startswith("data:"):
        return False
    return not any(h in u for h in BAD_IMAGE_HINTS)


def extract_feed_image(entry) -> str:
    """Pull the best artwork URL out of an RSS/Atom entry."""
    for key in ("media_thumbnail", "media_content"):
        for v in entry.get(key) or []:
            url = (v or {}).get("url", "")
            if plausible_image(url):
                return url
    for enc in entry.get("enclosures") or []:
        href = (enc or {}).get("href", "") or (enc or {}).get("url", "")
        if href and (str((enc or {}).get("type", "")).startswith("image") or href.lower().endswith(IMG_EXT)):
            if plausible_image(href):
                return href
    for chunk in (entry.get("content") or []):
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', (chunk or {}).get("value", "") or "")
        if m and plausible_image(m.group(1)):
            return m.group(1)
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', entry.get("summary", "") or entry.get("description", "") or "")
    if m and plausible_image(m.group(1)):
        return m.group(1)
    return ""


def extract_page_image(html: str, base_url: str) -> str:
    """Read OpenGraph / Twitter card artwork from an already-fetched page."""
    if not html:
        return ""
    head = html[:120000]
    for pat in OG_PATTERNS:
        m = re.search(pat, head, re.I)
        if m:
            url = m.group(1).strip().replace("&amp;", "&")
            url = urljoin(base_url, url)
            if plausible_image(url):
                return url
    return ""


def download_for_pdf(articles: list[Article], dest_dir: Path, settings: dict) -> int:
    """Save artwork locally so the PDF is self-contained.

    The emailed HTML keeps remote URLs (light message); the PDF embeds files.
    Returns the number of images saved.
    """
    if not settings.get("images_enabled", True):
        return 0
    timeout = settings.get("image_timeout", 12)
    max_bytes = int(settings.get("image_max_bytes", 4_000_000))
    ua = settings.get("user_agent", "Mozilla/5.0")
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for a in articles:
        if not a.image:
            continue
        try:
            resp = requests.get(a.image, timeout=timeout, headers={"User-Agent": ua}, stream=True)
            if resp.status_code != 200:
                continue
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("image"):
                continue
            data = resp.raw.read(max_bytes + 1, decode_content=True)
            if not data or len(data) > max_bytes:
                continue
            ext = ".jpg"
            for e in IMG_EXT:
                if e in ctype or a.image.lower().split("?")[0].endswith(e):
                    ext = ".jpg" if e == ".jpeg" else e
                    break
            name = hashlib.sha1(a.image.encode("utf-8")).hexdigest()[:16] + ext
            path = dest_dir / name
            path.write_bytes(data)
            a.image_local = str(path.resolve())
            saved += 1
        except Exception as e:
            log.debug("Image download failed for %s: %s", a.image[:70], e)
    log.info("Downloaded %d/%d article images for PDF", saved, sum(1 for a in articles if a.image))
    return saved
