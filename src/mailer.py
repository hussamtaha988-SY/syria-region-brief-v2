from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from utils import env

log = logging.getLogger("mailer")


def send_email(subject: str, html: str, plain: str, attachments: list[Path], from_name: str) -> None:
    user = env("GMAIL_USER")
    password = env("GMAIL_APP_PASSWORD")
    to_raw = env("DIGEST_TO")
    if not user or not password or not to_raw:
        raise RuntimeError("Missing GMAIL_USER, GMAIL_APP_PASSWORD, or DIGEST_TO secrets.")

    recipients = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, user))
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    for path in attachments:
        if not path or not path.exists():
            continue
        data = path.read_bytes()
        if path.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        elif path.suffix.lower() == ".html":
            maintype, subtype = "text", "html"
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    log.info("Email sent to %s", ", ".join(recipients))


def plain_text(items: list[dict], date: str) -> str:
    lines = [f"Syria & Region Brief — {date}", ""]
    for i in items:
        lines += [
            f"[{i['id']}] {i['title_en']}",
            f"Source: {i['source']}",
            i["summary_en"],
            "",
            i["title_ar"],
            i["summary_ar"],
            f"Link: {i['link']}",
            "-" * 60,
        ]
    return "\n".join(lines)
