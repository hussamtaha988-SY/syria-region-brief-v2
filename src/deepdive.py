from __future__ import annotations

"""Deep-dive responder.

Reads the mailbox for "DEEPDIVE <url>" requests (created by the button under
each item in the morning brief), builds a long-form bilingual report about that
one story, and emails it back. Everything runs on the same free tier.
"""

import argparse
import email
import imaplib
import json
import logging
import re
import sys
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import requests
import trafilatura

from feeds import Article, gnews_url
from mailer import send_email
from render import render_pdf
from summarize import _extract_json
from utils import clean_html, ensure_dir, env, load_config, local_date, truncate

log = logging.getLogger("deepdive")

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

SYSTEM = (
    "You are a rigorous bilingual research editor. You write a deep, neutral background "
    "report using ONLY the supplied material. You never invent facts, quotes, numbers or "
    "outcomes. You clearly separate what is reported from what is uncertain. You write "
    "Modern Standard Arabic and clear English."
)

REPORT_PROMPT = """Write an in-depth bilingual briefing about the single story below.

Return ONLY valid JSON with this exact shape:
{"title_ar":"...","title_en":"...","what_happened_ar":"...","what_happened_en":"...",
 "background_ar":"...","background_en":"...","key_players_ar":"...","key_players_en":"...",
 "why_it_matters_ar":"...","why_it_matters_en":"...","what_to_watch_ar":"...","what_to_watch_en":"...",
 "uncertainties_ar":"...","uncertainties_en":"..."}

Rules:
- Each *_ar / *_en field: about WORDS_TARGET words, factual and neutral.
- Use ONLY the main article and the related coverage supplied. No outside knowledge.
- "background": context explicitly present in the material.
- "key_players": people, institutions and states named in the material and their stated role.
- "why_it_matters": consequences stated or directly implied by the material.
- "what_to_watch": next steps, dates or decisions the material mentions.
- "uncertainties": what the material does NOT establish, conflicting claims between sources.
- If the material is thin for a field, say so plainly instead of padding.
- Do not copy sentences verbatim; paraphrase.

MAIN ARTICLE:
MAIN_JSON

RELATED COVERAGE:
RELATED_JSON
"""

SECTIONS = [
    ("what_happened", "ما الذي حدث", "What happened"),
    ("background", "الخلفية والسياق", "Background and context"),
    ("key_players", "الأطراف الرئيسية", "Key players"),
    ("why_it_matters", "لماذا يهم", "Why it matters"),
    ("what_to_watch", "ما يجب مراقبته", "What to watch"),
    ("uncertainties", "نقاط غير محسومة", "Uncertainties and conflicting claims"),
]


def _decode(value: str) -> str:
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out)


def _body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"
                    )
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return clean_html(
                        part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "replace"
                        )
                    )
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace"
        )
    except Exception:
        return ""


def collect_requests(cfg: dict, limit: int) -> list[tuple[bytes, str]]:
    """Return [(imap_id, url)] for pending deep-dive requests."""
    user = env("GMAIL_USER")
    password = env("GMAIL_APP_PASSWORD")
    if not user or not password:
        log.error("Missing GMAIL_USER or GMAIL_APP_PASSWORD.")
        return []
    found: list[tuple[bytes, str]] = []
    with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60) as imap:
        imap.login(user, password)
        imap.select("INBOX")
        status, data = imap.search(None, '(UNSEEN SUBJECT "Deep Dive")')
        if status != "OK":
            return []
        ids = data[0].split()
        log.info("Pending deep-dive requests: %d", len(ids))
        for mid in ids[:limit]:
            status, raw = imap.fetch(mid, "(RFC822)")
            if status != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            text = _decode(msg.get("Subject", "")) + "\n" + _body(msg)
            urls = [u for u in URL_RE.findall(text) if "news.google.com" not in u]
            if not urls:
                urls = URL_RE.findall(text)
            if not urls:
                log.warning("Request %s contained no URL; skipping.", mid)
                continue
            found.append((mid, urls[0].rstrip(".,;")))
        # Mark handled messages as seen so they are not processed twice.
        for mid, _ in found:
            imap.store(mid, "+FLAGS", "\\Seen")
    return found


def fetch_main(url: str, settings: dict) -> dict:
    ua = settings.get("user_agent", "Mozilla/5.0")
    title, text = "", ""
    try:
        resp = requests.get(url, timeout=settings.get("request_timeout", 20),
                            headers={"User-Agent": ua})
        if resp.status_code < 400:
            m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.S | re.I)
            if m:
                title = clean_html(m.group(1))
            extracted = trafilatura.extract(
                resp.text, include_comments=False, include_tables=False, favor_precision=True
            )
            if extracted:
                text = truncate(extracted, 9000)
    except Exception as e:
        log.warning("Main article fetch failed: %s", e)
    return {"url": url, "title": title, "text": text}


def fetch_related(title: str, cfg: dict, count: int) -> list[dict]:
    """Gather a few other outlets' takes on the same story via Google News."""
    import feedparser

    stop = {"the","a","an","in","on","at","of","and","to","for","with","after","over",
            "says","said","its","his","her","their","from","by","as","is","are","was",
            "في","من","على","عن","الى","إلى","مع","بعد","وقد","التي","الذي"}
    words = [w for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", title or "").split()
             if w.lower() not in stop and len(w) > 2]
    if not words:
        return []
    key = {w.lower() for w in words}
    out: list[dict] = []
    seen: set[str] = set()
    ua = cfg["settings"].get("user_agent", "Mozilla/5.0")
    # Long queries return nothing on Google News; shorten until we get coverage.
    for n in (8, 6, 4):
        query = " ".join(words[:n]) + " when:7d"
        try:
            resp = requests.get(
                gnews_url({"query": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}),
                timeout=20, headers={"User-Agent": ua},
            )
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            log.warning("Related coverage lookup failed: %s", e)
            continue
        for entry in parsed.entries[: count * 4]:
            link = entry.get("link", "")
            t = clean_html(entry.get("title", ""))
            if not link or not t or link in seen:
                continue
            t_words = {w.lower() for w in re.sub(r"[^\w\u0600-\u06FF\s]", " ", t).split()}
            overlap = len(key & t_words)
            regional = bool(t_words & {
                "syria","syrian","idlib","aleppo","homs","damascus","hasakah","raqqa",
                "lebanon","lebanese","iraq","iraqi","iran","iranian","turkey","turkish",
                "israel","israeli","gaza","jordan","egypt","saudi","gulf","middle","east",
                "سوريا","سورية","لبنان","العراق","إيران","ايران","تركيا","إسرائيل","غزة",
                "الأردن","مصر","السعودية","الخليج","الشرق","الأوسط","إدلب","حلب","دمشق",
            })
            # This is a Syria/region brief: require BOTH topic overlap and a
            # regional signal, otherwise unrelated world stories leak in.
            if overlap < 2 or not regional:
                continue
            seen.add(link)
            out.append({
                "title": t,
                "source": (entry.get("source") or {}).get("title", ""),
                "url": link,
                "snippet": clean_html(entry.get("summary", ""))[:700],
            })
            if len(out) >= count:
                return out
    return out


def build_report(main: dict, related: list[dict], words: int) -> dict | None:
    api_key = env("GEMINI_API_KEY")
    if not api_key:
        log.error("No GEMINI_API_KEY; cannot build a deep-dive report.")
        return None
    prompt = (
        REPORT_PROMPT.replace("WORDS_TARGET", str(words))
        .replace("MAIN_JSON", json.dumps(main, ensure_ascii=False))
        .replace("RELATED_JSON", json.dumps(related, ensure_ascii=False))
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.25,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }
    models = [env("GEMINI_MODEL", "gemini-3.1-flash-lite")]
    for m in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-3.8-flash"):
        if m not in models:
            models.append(m)
    for mdl in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        try:
            resp = requests.post(url, headers={"x-goog-api-key": api_key,
                                               "Content-Type": "application/json"},
                                 json=body, timeout=180)
            if resp.status_code != 200:
                log.warning("Deep dive: %s returned %s", mdl, resp.status_code)
                continue
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = _extract_json(text)
            if data.get("title_ar") or data.get("title_en"):
                log.info("Deep dive report built with %s", mdl)
                return data
        except Exception as e:
            log.warning("Deep dive model %s failed: %s", mdl, e)
    return None


def report_html(report: dict, main: dict, related: list[dict], date_str: str) -> str:
    from jinja2 import Template

    blocks = []
    for key, ar_label, en_label in SECTIONS:
        ar = report.get(f"{key}_ar", "")
        en = report.get(f"{key}_en", "")
        if not ar and not en:
            continue
        blocks.append(
            f'<div class="sec"><h3>{ar_label} · {en_label}</h3>'
            f'<p class="ar">{ar}</p><p class="en">{en}</p></div>'
        )
    rel = "".join(
        f'<li><a href="{r["url"]}">{r["title"]}</a>'
        f'<span class="src"> — {r.get("source","")}</span></li>'
        for r in related
    )
    tpl = Template("""<!doctype html><html lang="ar"><head><meta charset="utf-8">
<title>تقرير معمّق — {{ date }}</title><style>
body{margin:0;background:#f4f5f7;font-family:"Noto Sans Arabic","DejaVu Sans",Arial,sans-serif;color:#1a1d23}
.wrap{max-width:820px;margin:0 auto;padding:24px 16px}
.head{background:#0f2540;color:#fff;border-radius:12px;padding:24px}
.head h1{margin:0 0 8px;font-size:22px;line-height:1.5;direction:rtl;text-align:right}
.head .en{direction:ltr;text-align:left;font-size:15px;opacity:.9}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:22px;margin-top:16px}
.sec{border-top:1px solid #edf0f3;padding-top:14px;margin-top:14px}
.sec:first-child{border-top:none;padding-top:0;margin-top:0}
h3{font-size:16px;color:#0f2540;margin:0 0 8px}
p.ar{direction:rtl;text-align:right;font-size:15px;line-height:1.95;margin:6px 0}
p.en{direction:ltr;text-align:left;font-size:14px;line-height:1.75;margin:6px 0;color:#2a2f38}
a{color:#0b63ce;text-decoration:none;font-weight:600}
ul{padding-left:18px;font-size:13px;line-height:1.9}
.src{color:#6b7280;font-weight:400}
.foot{font-size:12px;color:#6b7280;margin-top:18px;line-height:1.8}
</style></head><body><div class="wrap">
<div class="head"><h1>{{ title_ar }}</h1><div class="en">{{ title_en }}</div>
<div class="en" style="margin-top:10px;font-size:13px">تقرير معمّق · Deep dive · {{ date }}</div></div>
<div class="card">{{ blocks|safe }}</div>
<div class="card"><h3>المقال الأصلي · Original article</h3>
<p class="en"><a href="{{ main_url }}">{{ main_title or main_url }}</a></p>
{% if rel %}<h3 style="margin-top:16px">تغطية ذات صلة · Related coverage</h3><ul>{{ rel|safe }}</ul>{% endif %}</div>
<div class="foot">تقرير آلي مبني على مواد منشورة علنًا، مع الإشارة إلى المصادر. لا يعيد نشر المقال كاملًا ولا يتجاوز جدران الدفع.<br>
Automated report built from publicly available material, with sources cited. It does not reproduce full articles or bypass paywalls.</div>
</div></body></html>""")
    return tpl.render(
        date=date_str,
        title_ar=report.get("title_ar", "تقرير معمّق"),
        title_en=report.get("title_en", "Deep dive"),
        blocks="".join(blocks),
        main_url=main["url"],
        main_title=main.get("title", ""),
        rel=rel,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    logging.getLogger("weasyprint").setLevel(logging.WARNING)
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--url", help="Build a report for this URL directly (manual trigger)")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--out", default=str(ROOT / "out"))
    args = p.parse_args()

    cfg = load_config(args.config)
    deep = cfg.get("deep_dive", {}) or {}
    if not deep.get("enabled", True):
        log.info("Deep dive disabled in config.")
        return 0
    settings = cfg["settings"]
    date_str = local_date(settings.get("timezone", "Asia/Dubai"))
    out_dir = ensure_dir(Path(args.out) / f"deep-dive-{date_str}")

    if args.url:
        requests_list = [(None, args.url)]
    else:
        requests_list = collect_requests(cfg, int(deep.get("max_requests_per_run", 3)))
    if not requests_list:
        log.info("No deep-dive requests. Nothing to do.")
        return 0

    for n, (_, url) in enumerate(requests_list, start=1):
        log.info("[%d/%d] Deep dive: %s", n, len(requests_list), url[:100])
        main_article = fetch_main(url, settings)
        if not main_article["text"]:
            log.warning("Could not read the article publicly; report will rely on related coverage.")
        related = fetch_related(main_article.get("title", ""), cfg, int(deep.get("related_items", 4)))
        report = build_report(main_article, related, int(deep.get("report_words", 600)))
        if not report:
            log.error("Report generation failed for %s", url)
            continue
        html = report_html(report, main_article, related, date_str)
        html_path = out_dir / f"deep-dive-{n}.html"
        html_path.write_text(html, encoding="utf-8")
        pdf_path = out_dir / f"deep-dive-{n}.pdf"
        pdf_ok = render_pdf(html, pdf_path)
        try:
            from premailer import transform
            email_html = transform(html, keep_style_tags=False, strip_important=False)
        except Exception:
            email_html = html
        subject = f"تقرير معمّق | Deep Dive — {report.get('title_ar', date_str)}"
        plain = "\n\n".join(
            f"{ar} / {en}\n{report.get(k + '_ar', '')}\n{report.get(k + '_en', '')}"
            for k, ar, en in SECTIONS
        ) + f"\n\nOriginal: {url}"
        try:
            send_email(
                subject=subject,
                html=email_html,
                plain=plain,
                attachments=[html_path] + ([pdf_path] if pdf_ok else []),
                from_name=cfg["email"].get("from_name", "Syria & Region Brief"),
            )
        except Exception as e:
            log.error("Deep-dive email failed: %s", e)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
