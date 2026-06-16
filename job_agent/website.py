"""Static GitHub Pages site — one self-contained index.html, no build step / no deps.

A dense, scannable list: every tier-worthy role is a COLLAPSED row (fit · title ·
company · location · pay · NEW) that expands on click (native <details>) to show
"Why it fits" / "Watch-outs" bullets, dates, and the Apply link. A small inline
<script> filters by tier / company / remote-only / pay-disclosed / free text.

Grounding: only real scored rows + real URLs render; HTML-escaped; nothing invented.
Output -> ./docs (GitHub Pages: main branch /docs).
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, store
from .digest import _bullets, _red_flags, _salary
from .tiers import ORDER, TIER_BADGES, TIER_TITLES, tier_for

SITE_DIR = config.BASE_DIR / "docs"

_SELECT = """
SELECT j.id, j.fingerprint, j.title, j.company, j.location, j.remote,
       j.salary_min, j.salary_max, j.salary_currency, j.posted_at, j.first_seen_at, j.url,
       s.fit_score, s.label, s.rationale, s.red_flags,
       (SELECT 1 FROM notifications n WHERE n.job_id = j.id) AS notified,
       (SELECT 1 FROM drafts d WHERE d.job_id = j.id) AS drafted
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
:root{--bg:#f5f5f3;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e4e4e1;
--strong:#1a7f4b;--look:#9a6b00;--new:#c0392b;--accent:#0a5ad6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:18px 14px 64px}
header h1{margin:0 0 2px;font-size:22px}.sub{margin:0;color:var(--muted);font-size:13px}.sub b{color:var(--ink)}
.filters{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;margin:10px 0 4px;
display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line)}
.filters input,.filters select{padding:6px 8px;border:1px solid var(--line);border-radius:7px;font-size:14px;background:#fff}
.filters input#q{flex:1;min-width:150px}
.filters label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:5px}
#count{color:var(--muted);font-size:13px;margin:4px 0 2px}
h2.tier{margin:22px 0 6px;font-size:17px}
details.role{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0}
details.role>summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
details.role>summary::-webkit-details-marker{display:none}
details.role>summary:hover{background:#fafaf8}
.fit{font-weight:700;font-variant-numeric:tabular-nums;border-radius:7px;padding:2px 7px;font-size:13px;color:#fff;flex:none}
.fit.s{background:var(--strong)}.fit.l{background:var(--look)}
.t{font-weight:600}.co{color:var(--muted)}.sp{flex:1 1 12px}
.m{color:var(--muted);font-size:12.5px}.pay{color:var(--strong);font-weight:600;font-size:12.5px}
.new{background:var(--new);color:#fff;border-radius:5px;padding:1px 6px;font-size:10.5px;font-weight:700}
.drafts{background:#e7f0ff;color:var(--accent);border:1px solid #cfe0ff;border-radius:5px;padding:1px 6px;font-size:10.5px;font-weight:700}
.body{padding:2px 14px 12px;border-top:1px solid var(--line);font-size:14px}
.body h4{margin:10px 0 4px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.body ul{margin:2px 0;padding-left:18px}.body li{margin:2px 0}
.apply{display:inline-block;margin-top:10px;color:var(--accent);font-weight:600;text-decoration:none}
.apply:hover{text-decoration:underline}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
@media(max-width:520px){.t{flex-basis:100%}}
"""

_JS = """
(function(){
  var q=document.getElementById('q'),ft=document.getElementById('f-tier'),
      fc=document.getElementById('f-co'),fr=document.getElementById('f-remote'),
      fp=document.getElementById('f-pay'),cnt=document.getElementById('count');
  var roles=[].slice.call(document.querySelectorAll('details.role'));
  var secs=[].slice.call(document.querySelectorAll('section.tier'));
  function apply(){
    var term=(q.value||'').toLowerCase(),shown=0;
    roles.forEach(function(r){
      var ok=(!ft.value||r.dataset.tier===ft.value)
        &&(!fc.value||r.dataset.co===fc.value)
        &&(!fr.checked||r.dataset.remote==='1')
        &&(!fp.checked||r.dataset.pay==='1')
        &&(!term||r.dataset.text.indexOf(term)>=0);
      r.style.display=ok?'':'none'; if(ok)shown++;
    });
    secs.forEach(function(s){
      var vis=[].slice.call(s.querySelectorAll('details.role')).some(function(r){return r.style.display!=='none';});
      s.style.display=vis?'':'none';
    });
    cnt.textContent=shown+' role'+(shown===1?'':'s')+' shown';
  }
  [q,ft,fc,fr,fp].forEach(function(el){el.addEventListener('input',apply);});
  apply();
})();
"""


def _date(s: Optional[str]) -> str:
    return str(s)[:10] if s else ""


def _attr(s: str) -> str:
    return html.escape(s or "", quote=True)


def _card(row: sqlite3.Row) -> str:
    tier = tier_for(row["fit_score"], row["label"])
    fit_cls = "s" if tier == "strong" else "l"
    fit = row["fit_score"] if row["fit_score"] is not None else "—"
    loc = row["location"] or ""
    pay = _salary(row)
    pros, cons = _bullets(row["rationale"]), _red_flags(row["red_flags"])
    is_new = not row["notified"]
    text = " ".join([row["title"] or "", row["company"] or "", loc, " ".join(pros), " ".join(cons)]).lower()

    summary_meta = "  ·  ".join(m for m in (html.escape(loc), f'posted {_date(row["posted_at"])}' if row["posted_at"] else "") if m)
    s = [
        f'<details class="role" data-tier="{tier}" data-co="{_attr(row["company"] or "")}" '
        f'data-remote="{1 if row["remote"] else 0}" data-pay="{1 if pay else 0}" data-text="{_attr(text)}">',
        "<summary>",
        f'<span class="fit {fit_cls}">{fit}</span>',
        f'<span class="t">{html.escape(row["title"] or "Untitled role")}</span>',
        f'<span class="co">{html.escape(row["company"] or "")}</span>',
        '<span class="sp"></span>',
    ]
    if pay:
        s.append(f'<span class="pay">{html.escape(pay)}</span>')
    if summary_meta:
        s.append(f'<span class="m">{summary_meta}</span>')
    if row["drafted"]:
        s.append('<span class="drafts">drafts ready</span>')
    if is_new:
        s.append('<span class="new">NEW</span>')
    s.append("</summary>")
    s.append('<div class="body">')
    if pros:
        s.append("<h4>Why it fits</h4><ul>" + "".join(f"<li>{html.escape(p)}</li>" for p in pros) + "</ul>")
    if cons:
        s.append("<h4>Watch-outs</h4><ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in cons) + "</ul>")
    s.append(f'<p class="m">first seen {_date(row["first_seen_at"])}</p>')
    if row["url"]:
        s.append(f'<a class="apply" href="{html.escape(row["url"])}" target="_blank" rel="noopener">Apply →</a>')
    s.append("</div></details>")
    return "".join(s)


def render_html(rows: List[sqlite3.Row], *, generated_at: Optional[datetime] = None) -> Tuple[str, dict]:
    generated_at = generated_at or datetime.now().astimezone()
    buckets = {t: [r for r in rows if tier_for(r["fit_score"], r["label"]) == t] for t in ORDER}
    companies = sorted({r["company"] for r in rows if tier_for(r["fit_score"], r["label"]) and r["company"]})
    stats = {
        "strong": len(buckets["strong"]),
        "look": len(buckets["look"]),
        "new": sum(1 for r in rows if not r["notified"] and tier_for(r["fit_score"], r["label"])),
        "companies": len(companies),
    }

    sections = []
    for t in ORDER:
        items = buckets[t]
        if not items:
            continue
        cards = "".join(_card(r) for r in items)
        sections.append(
            f'<section class="tier" data-tier="{t}"><h2 class="tier">{TIER_BADGES[t]} {TIER_TITLES[t]} '
            f'<span style="color:var(--muted);font-weight:400">({len(items)})</span></h2>{cards}</section>'
        )
    body = "".join(sections) or '<p class="m">No in-scope roles yet — run the pipeline.</p>'
    co_opts = "".join(f'<option value="{_attr(c)}">{html.escape(c)}</option>' for c in companies)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job matches — {generated_at:%Y-%m-%d}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<header><h1>Job matches</h1>
<p class="sub">Updated {generated_at:%Y-%m-%d %H:%M %Z} · <b>{stats['strong']}</b> strong · <b>{stats['look']}</b> worth a look · <b>{stats['new']}</b> new · {stats['companies']} companies</p></header>
<div class="filters">
<input id="q" type="search" placeholder="Search title / company / notes…">
<select id="f-tier"><option value="">All tiers</option><option value="strong">Strong</option><option value="look">Worth a look</option></select>
<select id="f-co"><option value="">All companies</option>{co_opts}</select>
<label><input type="checkbox" id="f-remote"> Remote only</label>
<label><input type="checkbox" id="f-pay"> Pay shown</label>
</div>
<div id="count"></div>
<main>{body}</main>
<footer>Generated by job-agent — grounded on real scored listings. Tiers: Strong ≥ {config.TIER_STRONG_MIN}, Worth a look {config.TIER_LOOK_MIN}–{config.TIER_STRONG_MIN - 1}. Click a row to expand.</footer>
<script>{_JS}</script>
</div></body></html>
"""
    return page, stats


def build_site(conn: sqlite3.Connection, *, generated_at: Optional[datetime] = None,
               min_score: Optional[int] = None) -> Tuple[Path, dict, List[sqlite3.Row]]:
    rows = select_master(conn, min_score)
    page, stats = render_html(rows, generated_at=generated_at)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")
    return SITE_DIR / "index.html", stats, rows


def mark_published(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> None:
    for r in rows:
        store.mark_fingerprint_notified(conn, r["fingerprint"], "site")
