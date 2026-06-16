"""Static GitHub Pages site generator — one self-contained index.html, no build step.

Renders the MASTER set (all tier-worthy, non-skip, non-dismissed roles accumulated in
SQLite) grouped by tier -> company, sorted by fit. Roles not yet marked notified get a
NEW badge (reuses seen-state). Grounding: only real scored rows with real URLs are
rendered; nothing is invented. Output goes to ./docs (GitHub Pages: main branch /docs).
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, store
from .digest import _red_flags, _salary
from .tiers import ORDER, TIER_BADGES, TIER_TITLES, tier_for

SITE_DIR = config.BASE_DIR / "docs"

_SELECT = """
SELECT j.id, j.fingerprint, j.title, j.company, j.location, j.remote,
       j.salary_min, j.salary_max, j.salary_currency, j.posted_at, j.first_seen_at, j.url,
       s.fit_score, s.label, s.rationale, s.red_flags,
       (SELECT 1 FROM notifications n WHERE n.job_id = j.id) AS notified
FROM jobs j
JOIN scores s ON s.id = (
    SELECT id FROM scores s2 WHERE s2.job_id = j.id AND s2.stage = 'deep'
    ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1
)
LEFT JOIN feedback f ON f.job_id = j.id
WHERE s.label != 'skip'
  AND COALESCE(s.fit_score, 0) >= :min_score
  AND (f.decision IS NULL OR f.decision != 'dismissed')
ORDER BY s.fit_score DESC, j.first_seen_at DESC
"""


def select_master(conn: sqlite3.Connection, min_score: Optional[int] = None) -> List[sqlite3.Row]:
    """All tier-worthy roles, deduped by fingerprint (best score per role)."""
    min_score = config.TIER_LOOK_MIN if min_score is None else min_score
    rows = conn.execute(_SELECT, {"min_score": min_score}).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["fingerprint"] in seen:
            continue
        seen.add(r["fingerprint"])
        out.append(r)
    return out


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e6e6e3;
--strong:#1a7f4b;--look:#9a6b00;--new:#c0392b;--accent:#0a5ad6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 64px}
header h1{margin:0 0 4px;font-size:26px}.sub{margin:0;color:var(--muted);font-size:14px}
.sub b{color:var(--ink)}
h2.tier{margin:34px 0 10px;font-size:20px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3.co{margin:20px 0 8px;font-size:15px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0;
box-shadow:0 1px 2px rgba(0,0,0,.04)}
.head{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap}
.fit{font-weight:700;font-variant-numeric:tabular-nums;border-radius:8px;padding:2px 8px;font-size:14px;color:#fff;flex:none}
.fit.s{background:var(--strong)}.fit.l{background:var(--look)}
.title{font-weight:650;font-size:16px;margin:0;flex:1 1 240px;min-width:200px}
.new{background:var(--new);color:#fff;border-radius:6px;padding:1px 7px;font-size:11px;font-weight:700;letter-spacing:.03em}
.lbl{color:var(--muted);font-size:12px;border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.meta{color:var(--muted);font-size:13px;margin:6px 0}
.why{margin:8px 0 6px;font-size:14.5px}
.flags{margin:6px 0;padding:8px 10px;background:#fbf6ee;border-left:3px solid var(--look);border-radius:6px;font-size:13px;color:#5b4a22}
.flags ul{margin:4px 0 0;padding-left:18px}
.apply{display:inline-block;margin-top:8px;color:var(--accent);font-weight:600;text-decoration:none;font-size:14px}
.apply:hover{text-decoration:underline}
.empty{color:var(--muted);padding:24px 0}
footer{margin-top:48px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
@media(max-width:520px){.wrap{padding:16px 12px 48px}.title{flex-basis:100%}}
"""


def _date(s: Optional[str]) -> str:
    return str(s)[:10] if s else ""


def _card(row: sqlite3.Row) -> str:
    tier = tier_for(row["fit_score"], row["label"])
    fit_cls = "s" if tier == "strong" else "l"
    fit = row["fit_score"] if row["fit_score"] is not None else "—"
    new = '<span class="new">NEW</span>' if not row["notified"] else ""
    meta = [m for m in (
        html.escape(row["location"] or ""),
        "Remote" if row["remote"] else "",
        _salary(row) or "",
        f"posted {_date(row['posted_at'])}" if row["posted_at"] else "",
        f"first seen {_date(row['first_seen_at'])}",
    ) if m]
    parts = [
        '<article class="card">',
        '<div class="head">',
        f'<span class="fit {fit_cls}">{fit}</span>',
        f'<p class="title">{html.escape(row["title"] or "Untitled role")}</p>',
        new,
        f'<span class="lbl">{html.escape(row["label"] or "")}</span>',
        "</div>",
        f'<p class="meta">{"  ·  ".join(meta)}</p>',
    ]
    if row["rationale"]:
        parts.append(f'<p class="why">{html.escape(row["rationale"])}</p>')
    flags = _red_flags(row["red_flags"])
    if flags:
        parts.append('<div class="flags"><b>Red flags</b><ul>'
                     + "".join(f"<li>{html.escape(f)}</li>" for f in flags) + "</ul></div>")
    if row["url"]:
        parts.append(f'<a class="apply" href="{html.escape(row["url"])}" target="_blank" rel="noopener">Apply →</a>')
    parts.append("</article>")
    return "".join(parts)


def render_html(rows: List[sqlite3.Row], *, generated_at: Optional[datetime] = None) -> Tuple[str, dict]:
    generated_at = generated_at or datetime.now().astimezone()
    buckets = {t: [] for t in ORDER}
    for r in rows:
        t = tier_for(r["fit_score"], r["label"])
        if t:
            buckets[t].append(r)
    stats = {
        "strong": len(buckets["strong"]),
        "look": len(buckets["look"]),
        "new": sum(1 for r in rows if not r["notified"] and tier_for(r["fit_score"], r["label"])),
        "companies": len({r["company"] for r in rows if tier_for(r["fit_score"], r["label"])}),
    }

    body = []
    for t in ORDER:
        items = buckets[t]
        if not items:
            continue
        body.append(f'<h2 class="tier">{TIER_BADGES[t]} {TIER_TITLES[t]} <span style="color:var(--muted);font-weight:400">({len(items)})</span></h2>')
        groups: "dict[str, List[sqlite3.Row]]" = {}
        for r in items:
            groups.setdefault(r["company"] or "Unknown company", []).append(r)
        for company, roles in groups.items():
            body.append(f'<h3 class="co">{html.escape(company)} · {len(roles)}</h3>')
            body.extend(_card(r) for r in roles)
    if not (stats["strong"] or stats["look"]):
        body.append('<p class="empty">No in-scope roles yet. Run the pipeline to populate.</p>')

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job matches — {generated_at:%Y-%m-%d}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<header>
<h1>Job matches</h1>
<p class="sub">Updated {generated_at:%Y-%m-%d %H:%M %Z} · <b>{stats['strong']}</b> strong · <b>{stats['look']}</b> worth a look · <b>{stats['new']}</b> new · {stats['companies']} companies</p>
</header>
<main>
{''.join(body)}
</main>
<footer>Generated by job-agent — grounded on real scored listings. Tiers: Strong ≥ {config.TIER_STRONG_MIN}, Worth a look {config.TIER_LOOK_MIN}–{config.TIER_STRONG_MIN - 1}.</footer>
</div></body></html>
"""
    return page, stats


def build_site(conn: sqlite3.Connection, *, generated_at: Optional[datetime] = None,
               min_score: Optional[int] = None) -> Tuple[Path, dict, List[sqlite3.Row]]:
    """Render + write ./docs/index.html. Returns (path, stats, rows). No git/seen-state side effects."""
    rows = select_master(conn, min_score)
    page, stats = render_html(rows, generated_at=generated_at)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")  # serve raw HTML, skip Jekyll
    return SITE_DIR / "index.html", stats, rows


def mark_published(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> None:
    """After a real publish, clear NEW state so the next run only badges fresh roles."""
    for r in rows:
        store.mark_fingerprint_notified(conn, r["fingerprint"], "site")
