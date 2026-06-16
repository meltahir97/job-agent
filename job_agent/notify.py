"""Optional Gmail-SMTP nudge. No-op (never crashes) if creds are missing or 0 new."""
from __future__ import annotations

from typing import List, Tuple

from . import config


def top_roles(rows, k: int = 3) -> List[str]:
    pick = [r for r in rows if not r["notified"]] or list(rows)
    return [f"{r['company']} – {r['title']} ({r['fit_score']})" for r in pick[:k]]


def render_nudge(stats: dict, site_url: str, tops: List[str]) -> Tuple[str, str]:
    subject = f"{stats['new']} new role(s) — {stats['strong']} strong, {stats['look']} worth a look"
    body = (
        f"{stats['new']} new role(s) — {stats['strong']} strong, {stats['look']} worth a look.\n\n"
        "Top 3:\n" + "\n".join(f"  • {t}" for t in tops) + "\n\n"
        f"Site: {site_url or '(set SITE_URL after enabling GitHub Pages)'}\n"
    )
    return subject, body


def send_nudge(stats: dict, site_url: str, tops: List[str]) -> str:
    """Returns a short status string (sent / skipped reason). Never raises."""
    user, pw, to = config.SMTP_USER, config.SMTP_APP_PASSWORD, config.NOTIFY_EMAIL
    if not to:
        return "skipped (no NOTIFY_EMAIL)"
    if not (user and pw):
        return "skipped (SMTP_USER / SMTP_APP_PASSWORD not set)"
    if stats.get("new", 0) <= 0:
        return "skipped (0 new roles)"
    try:
        import smtplib
        import ssl
        from email.message import EmailMessage

        subject, body = render_nudge(stats, site_url, tops)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(user, pw)
            s.send_message(msg)
        return f"sent to {to}"
    except Exception as e:  # never crash the pipeline over email
        return f"skipped (send failed: {type(e).__name__}: {e})"
