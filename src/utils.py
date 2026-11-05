from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
MULTISPACE = re.compile(r"\s+")


def load_config(path: str | Path) -> dict:
    load_dotenv()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_date(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def local_datetime(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return MULTISPACE.sub(" ", text).strip()


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = AR_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[^\w\u0600-\u06FF\s]", " ", text)
    return MULTISPACE.sub(" ", text).strip()


def contains_keyword(text_norm: str, keywords: list[str]) -> bool:
    for kw in keywords:
        kw_norm = normalize_text(kw)
        if not kw_norm:
            continue
        if " " in kw_norm:
            if kw_norm in text_norm:
                return True
        else:
            if re.search(r"(?<!\w)" + re.escape(kw_norm) + r"(?!\w)", text_norm):
                return True
    return False


def keyword_hits(text_norm: str, keywords: list[str]) -> int:
    hits = 0
    for kw in keywords:
        kw_norm = normalize_text(kw)
        if not kw_norm:
            continue
        if " " in kw_norm:
            if kw_norm in text_norm:
                hits += 1
        else:
            if re.search(r"(?<!\w)" + re.escape(kw_norm) + r"(?!\w)", text_norm):
                hits += 1
    return hits


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"
