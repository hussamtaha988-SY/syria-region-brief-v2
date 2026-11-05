from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from article_text import fetch_texts
from feeds import decode_google_links, fetch_all
from mailer import plain_text, send_email
from ranking import dedupe, score_article, select_articles
from render import build_items, render_html, render_pdf
from utils import ensure_dir, env, load_config, local_date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("weasyprint").setLevel(logging.WARNING)
log = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily bilingual Syria & region news brief")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--out", default=str(ROOT / "out"))
    p.add_argument("--dry-run", action="store_true", help="Generate files but do not send email")
    p.add_argument("--no-ai", action="store_true", help="Skip Gemini and use fallback summaries")
    p.add_argument("--max-items", type=int, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    settings = cfg["settings"]
    if args.max_items:
        settings["max_items"] = args.max_items
    elif env("MAX_ITEMS"):
        try:
            settings["max_items"] = int(env("MAX_ITEMS"))
        except ValueError:
            pass
    if args.no_ai:
        import os
        os.environ["NO_AI"] = "1"

    tz = settings.get("timezone", "Asia/Dubai")
    date_str = local_date(tz)
    out_dir = ensure_dir(Path(args.out) / date_str)

    log.info("Fetching sources…")
    raw = fetch_all(cfg["sources"], settings)
    log.info("Raw items: %d", len(raw))

    scored = [score_article(a, cfg["keywords"]) for a in raw]
    unique = dedupe(scored)
    selected = select_articles(unique, settings)
    log.info("Selected items: %d", len(selected))

    if not selected:
        log.error("No relevant articles found. Check feeds/keywords or try again later.")
        # Still produce a minimal issue so the user knows the run happened.
        items = []
        global_reads = []
        ai_used = False
    else:
        global_reads = []
        if settings.get("decode_google_links", True):
            log.info("Resolving Google News links…")
            decode_google_links(selected)
        log.info("Fetching public article text…")
        fetch_texts(selected, settings)
        global_reads = []
        if settings.get("further_reading_enabled", True):
            log.info("Finding verified free long reads from international publishers…")
            from further_reading import attach as attach_further
            global_reads = attach_further(selected, settings, int(settings.get("further_reading_per_item", 5)))
        log.info("Summarizing…")
        from summarize import summarize
        summaries, ai_used = summarize(selected)
        items = build_items(selected, summaries)

    # Email HTML keeps remote image URLs so the message stays light.
    html = render_html(items, cfg, ai_used, ROOT / "templates", global_reads=global_reads)
    html_path = out_dir / f"brief-{date_str}.html"
    html_path.write_text(html, encoding="utf-8")
    log.info("HTML saved: %s", html_path)

    # The PDF embeds downloaded artwork so it reads well offline / in print.
    pdf_ok = False
    pdf_path = out_dir / f"brief-{date_str}.pdf"
    if settings.get("images_enabled", True) and selected:
        try:
            from images import download_for_pdf
            download_for_pdf(selected, out_dir / "img", settings)
            # build_items() ran before the download, so refresh the local paths.
            local_by_id = {a.id: a.image_local for a in selected}
            for it in items:
                it["image_local"] = local_by_id.get(it["id"], "")
            pdf_html = render_html(items, cfg, ai_used, ROOT / "templates", local_images=True, global_reads=global_reads)
        except Exception as e:
            log.warning("Image embedding skipped: %s", e)
            pdf_html = html
    else:
        pdf_html = html
    pdf_ok = render_pdf(pdf_html, pdf_path)
    if pdf_ok:
        log.info("PDF saved: %s", pdf_path)

    json_path = out_dir / f"brief-{date_str}.json"
    json_path.write_text(json.dumps({
        "date": date_str,
        "ai_used": ai_used,
        "count": len(items),
        "international_count": sum(1 for a in selected if a.source_group == "international"),
        "global_long_reads": global_reads,
        "items": items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dry_run:
        log.info("Dry run: email not sent.")
        return 0

    subject = f"{cfg['email']['subject_prefix']} — {date_str}"
    attachments = [html_path]
    if pdf_ok:
        attachments.append(pdf_path)
    try:
        from premailer import transform
        email_html = transform(html, keep_style_tags=False, strip_important=False)
    except Exception:
        email_html = html
    try:
        send_email(
            subject=subject,
            html=email_html,
            plain=plain_text(items, date_str, global_reads=global_reads),
            attachments=attachments,
            from_name=cfg["email"].get("from_name", "Syria & Region Brief"),
        )
    except Exception as e:
        log.error("Email failed: %s", e)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
