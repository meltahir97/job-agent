"""Optional Gmail-SMTP nudge. No-op (never crashes) if creds are missing or 0 new."""
from __future__ import annotations

from typing import List, Optional, Tuple

from . import config


def top_roles(rows, k: int = 3) -> List[str]:
    pick = [r for r in rows if not r["notified"]] or list(rows)
    return [f"{r['company']} – {r['title']} ({r['fit_score']})" for r in pick[:k]]


def render_nudge(stats: dict, site_url: str, tops: List[str], *,
                 drafted_roles: Optional[List[str]] = None, proposals: int = 0) -> Tuple[str, str]:
    drafted_roles = drafted_roles or []
    new = stats.get("new", 0)
    if new == 0 and proposals:
        subject = f"{proposals} new company proposal(s) to review"
    else:
        subject = f"{new} new role(s) — {stats['strong']} strong, {stats['look']} worth a look"

    lines = [f"{new} new role(s) — {stats['strong']} strong, {stats['look']} worth a look.", ""]
    lines.append("Top 3:")
    lines += [f"  • {t}" for t in tops] or ["  (none)"]
    if drafted_roles:
        lines += ["", "Drafts ready for:"] + [f"  • {r}" for r in drafted_roles]
    if proposals:
        lines += ["", f"New company proposals: {proposals} — review on the site or `job-agent discover --list`."]
    lines += ["", f"Site: {site_url or '(set SITE_URL after enabling GitHub Pages)'}"]
    return subject, "\n".join(lines) + "\n"


def send_nudge(stats: dict, site_url: str, tops: List[str], *,
               drafted_roles: Optional[List[str]] = None, proposals: int = 0) -> str:
    """Returns a short status string (sent / skipped reason). Never raises."""
    user, pw, to = config.SMTP_USER, config.SMTP_APP_PASSWORD, config.NOTIFY_EMAIL
    if not to:
        return "skipped (no NOTIFY_EMAIL)"
    if not (user and pw):
        return "skipped (SMTP_USER / SMTP_APP_PASSWORD not set)"
    if stats.get("new", 0) <= 0 and proposals <= 0:
        return "skipped (0 new roles, 0 proposals)"
    try:
        import smtplib
        import ssl
        from email.message import EmailMessage

        subject, body = render_nudge(stats, site_url, tops, drafted_roles=drafted_roles, proposals=proposals)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(user, pw)
            s.send_message(msg)
        return f"sent to {to}"
    except Exception as e:  # never crash the pipeline over email
        return f"skipped (send failed: {type(e).__name__}: {e})"
