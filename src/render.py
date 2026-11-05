from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from feeds import Article
from utils import local_date, local_datetime

log = logging.getLogger("render")


def build_items(articles: list[Article], summaries: dict[str, dict]) -> list[dict]:
    items = []
    for a in articles:
        s = summaries.get(a.id, {})
        items.append({
            "id": a.id,
            "source": a.source,
            "source_lang": a.source_lang,
            "link": a.link,
            "published": a.published.strftime("%Y-%m-%d %H:%M UTC") if a.published else "",
            "tags": a.tags,
            "category": s.get("category", a.category),
            "limited": bool(s.get("limited", False)),
            "title_ar": s.get("title_ar", a.title),
            "title_en": s.get("title_en", a.title),
            "summary_ar": s.get("summary_ar", ""),
            "summary_en": s.get("summary_en", ""),
        })
    return items


def render_html(items: list[dict], cfg: dict, ai_used: bool, template_dir: str | Path) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("issue.html")
    tz = cfg["settings"].get("timezone", "Asia/Dubai")
    politics = [i for i in items if i["category"] != "economy"]
    economy = [i for i in items if i["category"] == "economy"]
    return template.render(
        date=local_date(tz),
        generated=local_datetime(tz),
        items=items,
        politics=politics,
        economy=economy,
        count=len(items),
        ai_used=ai_used,
        subject_prefix=cfg.get("email", {}).get("subject_prefix", "Syria & Region Brief"),
    )


def render_pdf(html: str, out_pdf: str | Path) -> bool:
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(out_pdf))
        return True
    except Exception as e:
        log.warning("PDF generation failed: %s", e)
        return False
