from __future__ import annotations

import json
import logging
import re
import time

import requests

from feeds import Article
from utils import env, truncate

log = logging.getLogger("summarize")

SYSTEM = (
    "You are a careful bilingual news editor. You summarize ONLY the supplied article text. "
    "You never invent facts, quotes, numbers, or outcomes. You write neutral, factual Modern Standard Arabic and clear English."
)

PROMPT_TEMPLATE = """Create a bilingual daily news brief about Syria and the surrounding region.

For EACH article below, return one JSON object with:
- id: the same id
- title_ar: concise Arabic headline
- title_en: concise English headline
- summary_ar: 70-100 words in Modern Standard Arabic, factual, neutral
- summary_en: 70-100 words in English, factual, neutral
- category: "politics" or "economy"
- limited: true if the supplied text is too short to summarize reliably, otherwise false

Rules:
- Use ONLY the supplied title/snippet/text. Do not add outside knowledge.
- If the text is insufficient, set limited=true and summarize only what is explicitly present.
- Do not reproduce the article verbatim; paraphrase.
- Keep names of people, places, and organizations accurate.
- Return ONLY valid JSON: {"items":[...]}

Articles:
{articles_json}
"""


def _payload(articles: list[Article]) -> list[dict]:
    out = []
    for a in articles:
        out.append({
            "id": a.id,
            "source": a.source,
            "language": a.source_lang,
            "title": a.title,
            "url": a.link,
            "snippet": a.snippet,
            "text": a.text or "",
            "category_hint": a.category,
        })
    return out


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def gemini_summarize(articles: list[Article]) -> dict[str, dict] | None:
    api_key = env("GEMINI_API_KEY")
    if not api_key or env("NO_AI") == "1" or not articles:
        return None

    model = env("GEMINI_MODEL", "gemini-2.5-flash")
    models = [model]
    for fallback in ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"):
        if fallback not in models:
            models.append(fallback)

    # NOTE: use replace(), not str.format(): the template contains literal JSON
    # braces ({"items":[...]}) which str.format() would treat as placeholders.
    prompt = PROMPT_TEMPLATE.replace(
        "{articles_json}", json.dumps(_payload(articles), ensure_ascii=False)
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }

    for mdl in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        for attempt in range(2):
            try:
                resp = requests.post(
                    url,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json=body,
                    timeout=120,
                )
                if resp.status_code == 429:
                    log.warning("Gemini quota/rate limit on %s; attempt %d", mdl, attempt + 1)
                    time.sleep(15 * (attempt + 1))
                    continue
                if resp.status_code in (404, 400):
                    log.warning("Gemini model %s unavailable (%s); trying fallback", mdl, resp.status_code)
                    break
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = _extract_json(text)
                items = parsed.get("items", parsed if isinstance(parsed, list) else [])
                result = {}
                for item in items:
                    if isinstance(item, dict) and item.get("id"):
                        result[item["id"]] = item
                if result:
                    log.info("Gemini summarized %d items with %s", len(result), mdl)
                    return result
            except Exception as e:
                log.warning("Gemini error on %s attempt %d: %s", mdl, attempt + 1, e)
                time.sleep(5)
    return None


def fallback_summaries(articles: list[Article]) -> dict[str, dict]:
    """No-AI fallback. Keeps the issue flowing without paid services."""
    result = {}
    for a in articles:
        base = truncate(a.text or a.snippet or a.title, 650)
        if a.source_lang == "ar":
            summary_ar = base or "لا يتوفر نص كافٍ للتلخيص."
            summary_en = "[Limited fallback mode: Arabic source text is available at the original link. Enable Gemini for an English summary.]"
            title_ar = a.title
            title_en = a.title
        else:
            summary_en = base or "Not enough text to summarize."
            summary_ar = "[وضع الاحتياط: النص الإنجليزي متاح عبر الرابط الأصلي. فعّل Gemini للحصول على ملخص عربي.]"
            title_ar = a.title
            title_en = a.title
        result[a.id] = {
            "id": a.id,
            "title_ar": title_ar,
            "title_en": title_en,
            "summary_ar": summary_ar,
            "summary_en": summary_en,
            "category": a.category,
            "limited": True,
        }
    return result


def summarize(articles: list[Article]) -> tuple[dict[str, dict], bool]:
    try:
        ai = gemini_summarize(articles)
    except Exception as e:
        # Never let an AI-side problem abort the whole issue.
        log.warning("Gemini step failed (%s); using fallback summaries.", e)
        ai = None
    if ai:
        # Fill any missing items with fallback.
        fb = fallback_summaries(articles)
        for a in articles:
            if a.id not in ai:
                ai[a.id] = fb[a.id]
        return ai, True
    return fallback_summaries(articles), False
