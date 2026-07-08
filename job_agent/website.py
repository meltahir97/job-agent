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
       (SELECT 1 FROM drafts d WHERE d.job_id = j.id) AS drafted,
       (SELECT drive_url FROM drafts d WHERE d.job_id = j.id) AS draft_url
FROM jobs j
JOIN scores s ON s.id = (
    SELECT id FROM scores s2 WHERE s2.job_id = j.id AND s2.stage = 'deep'
    ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1
)
LEFT JOIN feedback f ON f.job_id = j.id
WHERE (f.decision IS NULL OR f.decision != 'dismissed')
  {tier_filter}
ORDER BY s.fit_score DESC, j.first_seen_at DESC
"""

_TIER_FILTER = "AND s.label != 'skip' AND COALESCE(s.fit_score, 0) >= :min_score"


def _dedup(rows):
    seen, out = set(), []
    for r in rows:
        if r["fingerprint"] in seen:
            continue
        seen.add(r["fingerprint"])
        out.append(r)
    return out


def _cap_per_company(rows, cap):
    """Keep the strongest `cap` roles per company (rows are already fit-desc) so one
    high-volume board can't dominate. cap<=0 disables."""
    if not cap or cap <= 0:
        return rows
    seen, out = {}, []
    for r in rows:
        co = r["company"] or ""
        if seen.get(co, 0) >= cap:
            continue
        seen[co] = seen.get(co, 0) + 1
        out.append(r)
    return out


def select_master(conn: sqlite3.Connection, min_score: Optional[int] = None,
                  per_company_cap: Optional[int] = None) -> List[sqlite3.Row]:
    """Tier-worthy roles, deduped by fingerprint (best score per role), capped per
    company so no single board floods the list."""
    min_score = config.TIER_LOOK_MIN if min_score is None else min_score
    cap = config.PER_COMPANY_CAP if per_company_cap is None else per_company_cap
    sql = _SELECT.format(tier_filter=_TIER_FILTER)
    return _cap_per_company(_dedup(conn.execute(sql, {"min_score": min_score}).fetchall()), cap)


def select_all_scored(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every deep-scored role (incl. non-matches / skip / low fit), minus dismissed —
    so the user can draft for roles the agent didn't flag as a match."""
    sql = _SELECT.format(tier_filter="")
    return _dedup(conn.execute(sql).fetchall())


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
code{background:#ececeb;border-radius:4px;padding:1px 5px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.help{background:#f0f4ff;border:1px solid #d8e3ff;border-radius:8px;padding:9px 12px;font-size:12.5px;color:#33415a;margin:6px 0;line-height:1.6}
.act{color:var(--muted);font-size:12px;margin-top:8px;line-height:1.7}
h2.tier{margin:22px 0 6px;font-size:17px}
details.role{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0}
details.role>summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
details.role>summary::-webkit-details-marker{display:none}
details.role>summary:hover{background:#fafaf8}
.fit{font-weight:700;font-variant-numeric:tabular-nums;border-radius:7px;padding:2px 7px;font-size:13px;color:#fff;flex:none}
.fit.s{background:var(--strong)}.fit.l{background:var(--look)}.fit.o{background:#9aa0a6}
.t{font-weight:600}.co{color:var(--muted)}.sp{flex:1 1 12px}
.m{color:var(--muted);font-size:12.5px}.pay{color:var(--strong);font-weight:600;font-size:12.5px}
.new{background:var(--new);color:#fff;border-radius:5px;padding:1px 6px;font-size:10.5px;font-weight:700}
.drafts{background:#e7f0ff;color:var(--accent);border:1px solid #cfe0ff;border-radius:5px;padding:1px 6px;font-size:10.5px;font-weight:700}
.body{padding:2px 14px 12px;border-top:1px solid var(--line);font-size:14px}
.body h4{margin:10px 0 4px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.body ul{margin:2px 0;padding-left:18px}.body li{margin:2px 0}
.apply{display:inline-block;margin-top:10px;color:var(--accent);font-weight:600;text-decoration:none}
.apply:hover{text-decoration:underline}
.consider{margin-top:26px}
.sug{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:6px 0}
.sug .cmd{font-size:12px;color:var(--muted);margin-top:4px}
.sug code{background:#f0f0ee;border-radius:4px;padding:1px 5px;font-size:12px}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
.acts{display:inline-flex;gap:4px;margin-left:4px;flex:none}
.btn{font:600 11px/1 -apple-system,system-ui,sans-serif;border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 9px;cursor:pointer;color:var(--ink)}
.btn:hover{background:#f3f3f1}.btn.rej:hover{border-color:var(--new);color:var(--new)}
.btn.sav:hover{border-color:var(--strong);color:var(--strong)}.btn.app{border-color:var(--strong);color:var(--strong)}
.btn.draft:hover{border-color:var(--accent);color:var(--accent)}.draftlink{text-decoration:none;border-color:var(--accent);color:var(--accent)}.draftlink:hover{background:#eef4ff}
.btn[disabled]{opacity:.5;cursor:default}.savedtag{color:var(--strong);font-weight:700;font-size:12px}
details.role.is-saved{box-shadow:0 0 0 2px var(--strong) inset}
details.role.removing,.sug.removing{opacity:0;transform:translateY(-6px);transition:opacity .2s,transform .2s}
.sugact{display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap}
.slugform{display:flex;gap:6px;align-items:center;flex-wrap:wrap;width:100%;margin-top:6px}
.slugform input,.slugform select{padding:4px 6px;border:1px solid var(--line);border-radius:6px;font-size:12px}
.sugmsg{font-size:12px;color:var(--new)}
.appcard{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--strong);border-radius:8px;padding:10px 12px;margin:6px 0}
.apphead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.appst{padding:4px 6px;border:1px solid var(--line);border-radius:6px;font-size:12.5px;background:#fff}
.appmeta{color:var(--muted);font-size:12.5px;margin-top:4px}
.appmeta a{color:var(--accent);text-decoration:none}.appmeta a:hover{text-decoration:underline}
.appmeta .btn{padding:2px 7px;font-size:10.5px}
details.notes{margin-top:6px}details.notes>summary{cursor:pointer;font-size:12.5px;color:var(--muted);list-style:none}
details.notes>summary::-webkit-details-marker{display:none}
details.notes>summary:before{content:'▸ '}details.notes[open]>summary:before{content:'▾ '}
.notelist{list-style:none;margin:6px 0 0;padding:0}
.notelist li{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px dashed var(--line);font-size:13.5px}
.notelist li>span:first-child,.notelist li>label{flex:1}
.notelist li.todo.done>label{text-decoration:line-through;color:var(--muted)}
.notelist .when{color:var(--muted);font-size:11.5px;flex:none}
.notedel{padding:1px 6px;font-size:11px;color:var(--muted)}
.noteform{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.noteform input{flex:1;min-width:180px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px}
details.watchlist{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:0}
details.watchlist>summary{cursor:pointer;padding:10px 12px;font-weight:600;font-size:14px;list-style:none}
details.watchlist>summary::-webkit-details-marker{display:none}
details.watchlist>summary:before{content:'▸ '}details.watchlist[open]>summary:before{content:'▾ '}
.feedwrap{padding:0 12px 10px}
.feedrow{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;padding:5px 0;border-top:1px dashed var(--line);font-size:13px}
.feedrow .dot{flex:none;font-size:11px}.feedrow .dot.ok{color:var(--strong)}.feedrow .dot.bad{color:var(--new)}
.feedrow .board{color:var(--muted);font-size:11.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.feedrow .nums{color:var(--muted);font-size:12px}.feedrow .nums b{color:var(--ink)}
.feedrow.bad .nums{opacity:.6}
.feedwhy{flex-basis:100%;color:var(--new);font-size:11.5px;padding-left:18px}
.feedlegend{color:var(--muted);font-size:11.5px;padding:8px 0 2px}
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
  window.applyFilters=apply;
  apply();
})();
"""

# Only included by the local interactive app (job-agent serve); posts decisions to the API.
_ACTIONS_JS = """
(function(){
  function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(function(r){return r.json();});}
  document.addEventListener('click',function(e){
    var b=e.target.closest('button.btn'); if(!b) return;
    e.preventDefault(); e.stopPropagation();
    if(b.dataset.kind==='job'){
      var id=b.dataset.id, act=b.dataset.act, card=b.closest('details.role');
      b.disabled=true;
      post('/api/job/'+id+'/'+act).then(function(res){
        if(!res.ok){b.disabled=false; b.textContent='retry'; return;}
        if(act==='reject'){ card.classList.add('removing'); setTimeout(function(){card.remove(); if(window.applyFilters)window.applyFilters();},220); }
        else if(act==='save'){ card.classList.add('is-saved'); b.outerHTML='<span class="savedtag">Saved \\u2713</span> <button class="btn" data-kind="job" data-id="'+id+'" data-act="undo" title="Undo save">undo</button>'; }
        else if(act==='undo'||act==='applied'||act==='unapply'){ location.reload(); }
      });
    } else if(b.dataset.kind==='appnote'){
      var card=b.closest('.appcard'), jid=b.dataset.id, inp=card.querySelector('.notetxt'), txt=(inp.value||'').trim();
      if(!txt){ inp.focus(); return; }
      b.disabled=true;
      post('/api/job/'+jid+'/note',{text:txt,kind:b.dataset.noteKind}).then(function(res){
        b.disabled=false; if(!res.ok){ alert(res.error||'failed'); return; }
        inp.value='';
        var ul=card.querySelector('.notelist'), li=document.createElement('li');
        var del=' <button class="btn notedel" data-kind="notedel" data-id="'+res.id+'" title="Delete">\\u00d7</button>';
        if(res.kind==='todo'){ li.className='todo'; li.innerHTML='<label><input type="checkbox" class="tddone" data-id="'+res.id+'"> </label><span class="when">now</span>'+del; li.querySelector('label').append(' '+txt); }
        else { li.innerHTML='<span></span><span class="when">now</span>'+del; li.querySelector('span').textContent=txt; }
        ul.appendChild(li);
        var sum=card.querySelector('details.notes>summary'); if(sum) sum.textContent='Notes & to-dos ('+ul.children.length+')';
      });
    } else if(b.dataset.kind==='notedel'){
      post('/api/note/'+b.dataset.id+'/delete').then(function(res){ if(res.ok){ var li=b.closest('li'); if(li) li.remove(); } });
    } else if(b.dataset.kind==='draft'){
      var jid=b.dataset.id; b.disabled=true; b.textContent='Drafting…';
      post('/api/job/'+jid+'/draft').then(function(res){
        if(!res.ok){ b.disabled=false; b.textContent='retry'; b.title=res.error||'failed'; if(res.error)alert(res.error); return; }
        if(!/^https?:/.test(res.folder||'')){ b.textContent='Saved locally'; b.disabled=true; return; }
        var a=document.createElement('a'); a.className='btn draftlink'; a.href=res.folder;
        a.target='_blank'; a.rel='noopener'; a.textContent='📄 Drafts';
        b.replaceWith(a);
      });
    } else if(b.dataset.kind==='sug'){
      var wrap=b.closest('.sug'), sid=wrap.dataset.id, act=b.dataset.act, msg=wrap.querySelector('.sugmsg');
      b.disabled=true; if(act==='approve' && msg){ msg.textContent='adding…'; }
      post('/api/suggestion/'+sid+'/'+act).then(function(res){  // server auto-detects the board — no slug needed
        if(!res.ok){ b.disabled=false; if(msg)msg.textContent=(res.message||res.error||'error'); return; }
        wrap.classList.add('removing'); setTimeout(function(){wrap.remove();},220);
      });
    }
  });
  document.addEventListener('change',function(e){
    var t=e.target;
    if(t.classList&&t.classList.contains('appst')){
      post('/api/job/'+t.dataset.id+'/status',{status:t.value});
    } else if(t.classList&&t.classList.contains('tddone')){
      post('/api/note/'+t.dataset.id+'/toggle',{done:t.checked});
      var li=t.closest('li'); if(li)li.classList.toggle('done',t.checked);
    }
  });
  var fa=document.getElementById('f-all');
  if(fa){ fa.addEventListener('change',function(){ var p=new URLSearchParams(location.search); if(fa.checked){p.set('all','1');}else{p.delete('all');} location.search=p.toString(); }); }
})();
"""


def _date(s: Optional[str]) -> str:
    return str(s)[:10] if s else ""


def _attr(s: str) -> str:
    return html.escape(s or "", quote=True)


def _card(row: sqlite3.Row, interactive: bool = False) -> str:
    tier = tier_for(row["fit_score"], row["label"])
    fit_cls = {"strong": "s", "look": "l"}.get(tier, "o")
    data_tier = tier or "other"
    fit = row["fit_score"] if row["fit_score"] is not None else "—"
    loc = row["location"] or ""
    pay = _salary(row)
    pros, cons = _bullets(row["rationale"]), _red_flags(row["red_flags"])
    is_new = not row["notified"]
    text = " ".join([row["title"] or "", row["company"] or "", loc, " ".join(pros), " ".join(cons)]).lower()

    summary_meta = "  ·  ".join(m for m in (html.escape(loc), f'posted {_date(row["posted_at"])}' if row["posted_at"] else "") if m)
    s = [
        f'<details class="role" data-tier="{data_tier}" data-co="{_attr(row["company"] or "")}" '
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
    if interactive:
        if row["draft_url"]:
            draft_el = (f'<a class="btn draftlink" href="{html.escape(row["draft_url"])}" target="_blank" '
                        'rel="noopener" title="Open the drafts in Google Drive">📄 Drafts</a>')
        else:
            draft_el = (f'<button class="btn draft" data-kind="draft" data-id="{row["id"]}" '
                        'title="Write a tailored resume + cover letter to Drive">✎ Draft</button>')
        s.append(
            '<span class="acts">' + draft_el
            + f'<button class="btn rej" data-kind="job" data-id="{row["id"]}" data-act="reject" title="Hide this role">Reject</button>'
            + f'<button class="btn sav" data-kind="job" data-id="{row["id"]}" data-act="save" title="Mark interesting">Save</button>'
            + f'<button class="btn app" data-kind="job" data-id="{row["id"]}" data-act="applied" '
              'title="I applied — start tracking this application">Applied</button>'
            + "</span>"
        )
    s.append("</summary>")
    s.append('<div class="body">')
    if pros:
        s.append("<h4>Why it fits</h4><ul>" + "".join(f"<li>{html.escape(p)}</li>" for p in pros) + "</ul>")
    if cons:
        s.append("<h4>Watch-outs</h4><ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in cons) + "</ul>")
    s.append(f'<p class="m">id {row["id"]} · first seen {_date(row["first_seen_at"])}</p>')
    if row["url"]:
        s.append(f'<a class="apply" href="{html.escape(row["url"])}" target="_blank" rel="noopener">Apply →</a>')
    if not interactive:  # static page: show the terminal commands (buttons handle it in the local app)
        s.append(f'<p class="act">Not a fit? <code>job-agent reject {row["id"]}</code> · '
                 f'Interested? <code>job-agent save {row["id"]}</code></p>')
    s.append("</div></details>")
    return "".join(s)


def _watchlist_section(feed, funnel) -> str:
    """'Watchlist health' — every tracked company: is its feed live, how many roles
    came through the last fetch, and the scoring funnel (in DB → scored → surfaced).
    Local app only."""
    if not feed:
        return ""
    live = sum(1 for f in feed if f["ok"])
    checked = max((f["checked_at"] or "" for f in feed), default="")[:16].replace("T", " ")
    rows = []
    for f in sorted(feed, key=lambda r: ((r["ok"], (r["company"] or "").lower()))):
        agg = funnel.get(f["company"])
        board = (f'{f["ats"]}:{f["slug"]}' if f["ats"] and f["slug"]
                 else (f["ats"] or "?"))
        dot = '<span class="dot ok">●</span>' if f["ok"] else '<span class="dot bad">●</span>'
        nums = (f'fetched <b>{f["fetched"]}</b> · in-scope <b>{f["kept"]}</b>'
                + (f' · in DB <b>{agg["in_db"]}</b> · scored <b>{agg["deep_scored"] or 0}</b>'
                   f' · surfaced <b>{agg["surfaced"] or 0}</b>' if agg else "")
                + ((f' · applied <b>{agg["applied"]}</b>') if agg and agg["applied"] else "")
                + ((f' · rejected {agg["rejected"]}') if agg and agg["rejected"] else ""))
        why = ("" if f["ok"] else
               f'<div class="feedwhy">{html.escape(f["error"] or "no public job feed found yet")}</div>')
        rows.append(f'<div class="feedrow{"" if f["ok"] else " bad"}">{dot} '
                    f'<b>{html.escape(f["company"])}</b> <span class="board">{html.escape(board)}</span>'
                    f'<span class="sp"></span><span class="nums">{nums}</span>{why}</div>')
    legend = ('<div class="feedlegend">fetched = roles the feed returned last run · in-scope = after the '
              'Bay-Area/remote filter · in DB = stored all-time · scored = deep-scored · surfaced = made a tier. '
              'Red rows have no working feed — the agent re-checks them every run.</div>')
    return ('<details class="watchlist"><summary>🏢 Watchlist health '
            f'<span style="color:var(--muted);font-weight:400">({live}/{len(feed)} feeds live · '
            f'checked {html.escape(checked)})</span></summary>'
            f'<div class="feedwrap">{legend}{"".join(rows)}</div></details>')


_STATUS_LABELS = (("applied", "Applied"), ("interviewing", "Interviewing"), ("offer", "Offer"),
                  ("rejected", "Rejected"), ("withdrawn", "Withdrawn"))


def _applications_section(apps, notes_by_job) -> str:
    """'Applications' — roles the user marked Applied, with a status pipeline and a
    per-application notes / to-dos dialog. LOCAL APP ONLY (never on the public site)."""
    if not apps:
        return ""
    cards = []
    for a in apps:
        opts = "".join(f'<option value="{v}"{" selected" if a["status"] == v else ""}>{lbl}</option>'
                       for v, lbl in _STATUS_LABELS)
        items = []
        for n in notes_by_job.get(a["id"], []):
            when = f'<span class="when">{_date(n["created_at"])}</span>'
            delete = (f'<button class="btn notedel" data-kind="notedel" data-id="{n["id"]}" '
                      'title="Delete">×</button>')
            if n["kind"] == "todo":
                chk = " checked" if n["done"] else ""
                items.append(f'<li class="todo{" done" if n["done"] else ""}">'
                             f'<label><input type="checkbox" class="tddone" data-id="{n["id"]}"{chk}> '
                             f'{html.escape(n["text"])}</label>{when}{delete}</li>')
            else:
                items.append(f'<li><span>{html.escape(n["text"])}</span>{when}{delete}</li>')
        todos = (f' · <b>{a["open_todos"]}</b> to-do{"s" if a["open_todos"] != 1 else ""} open'
                 if a["open_todos"] else "")
        links = []
        if a["draft_url"]:
            links.append(f'<a href="{html.escape(a["draft_url"])}" target="_blank" rel="noopener">Drafts ↗</a>')
        if a["url"]:
            links.append(f'<a href="{html.escape(a["url"])}" target="_blank" rel="noopener">Listing ↗</a>')
        link_html = (" · " + " · ".join(links)) if links else ""
        cards.append(
            f'<div class="appcard" data-id="{a["id"]}">'
            f'<div class="apphead"><b>{html.escape(a["title"] or "Untitled role")}</b>'
            f'<span class="co">{html.escape(a["company"] or "")}</span><span class="sp"></span>'
            f'<select class="appst" data-id="{a["id"]}" title="Application status">{opts}</select></div>'
            f'<div class="appmeta">applied {_date(a["applied_at"])}{todos}{link_html} · '
            f'<button class="btn" data-kind="job" data-id="{a["id"]}" data-act="unapply" '
            'title="Remove from tracking (notes are kept)">untrack</button></div>'
            f'<details class="notes"><summary>Notes &amp; to-dos ({len(notes_by_job.get(a["id"], []))})</summary>'
            f'<ul class="notelist">{"".join(items)}</ul>'
            '<div class="noteform"><input type="text" class="notetxt" maxlength="500" '
            'placeholder="Add an update, note, or to-do…">'
            f'<button class="btn" data-kind="appnote" data-note-kind="note" data-id="{a["id"]}">+ Note</button>'
            f'<button class="btn" data-kind="appnote" data-note-kind="todo" data-id="{a["id"]}">+ To-do</button>'
            "</div></details></div>"
        )
    return ('<section class="pipeline"><h2 class="tier">📌 Applications '
            f'<span style="color:var(--muted);font-weight:400">({len(apps)})</span></h2>'
            + "".join(cards) + "</section>")


def _suggestions_section(suggestions, interactive: bool = False) -> str:
    """'Companies to consider' — propose-only discovery results with cited evidence."""
    if not suggestions:
        return ""
    cards = []
    for s in suggestions:
        board = f'{s["ats"]}:{s["slug"]}' if s["ats"] else "careers page"
        ev = s["evidence_url"] or ""
        link = f' · <a href="{html.escape(ev)}" target="_blank" rel="noopener">source ↗</a>' if ev else ""
        if interactive:
            action = (
                '<div class="sugact">'
                '<button class="btn app" data-kind="sug" data-act="approve" title="Add to watchlist">Approve</button>'
                '<button class="btn dis" data-kind="sug" data-act="dismiss" title="Hide this proposal">Dismiss</button>'
                '<span class="sugmsg"></span></div>'
            )
        else:
            action = (f'<div class="cmd">approve <code>job-agent approve {s["id"]}</code> · '
                      f'dismiss <code>job-agent dismiss {s["id"]}</code></div>')
        cards.append(
            f'<div class="sug" data-id="{s["id"]}" data-ats="{_attr(s["ats"] or "")}" data-slug="{_attr(s["slug"] or "")}">'
            f'<div><b>{html.escape(s["company"])}</b> <span class="m">{html.escape(board)}</span>{link}</div>'
            f'<div class="m">{html.escape(s["reason"] or "")}</div>'
            f'{action}</div>'
        )
    note = ("Verified to a real feed or careers page. Click <b>Approve</b> to add one to your "
            "watchlist (its job board is detected automatically) or <b>Dismiss</b> to hide it. "
            "Nothing is added automatically."
            if interactive else
            "Proposed by weekly discovery — verified to a real feed or careers page. In your "
            "terminal, run <code>job-agent approve &lt;id&gt;</code> to add one or "
            "<code>job-agent dismiss &lt;id&gt;</code> to hide it. Nothing is added automatically.")
    return (
        '<section class="consider"><h2 class="tier">🧭 Companies to consider '
        f'<span style="color:var(--muted);font-weight:400">({len(suggestions)})</span></h2>'
        f'<p class="m">{note}</p>' + "".join(cards) + "</section>"
    )


def render_html(rows: List[sqlite3.Row], *, generated_at: Optional[datetime] = None,
                suggestions=None, interactive: bool = False, include_all: bool = False,
                applications=None, app_notes=None, feed=None, funnel=None) -> Tuple[str, dict]:
    generated_at = generated_at or datetime.now().astimezone()
    buckets = {t: [r for r in rows if tier_for(r["fit_score"], r["label"]) == t] for t in ORDER}
    other = [r for r in rows if tier_for(r["fit_score"], r["label"]) is None]
    companies = sorted({r["company"] for r in rows if r["company"]})
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
        cards = "".join(_card(r, interactive) for r in items)
        sections.append(
            f'<section class="tier" data-tier="{t}"><h2 class="tier">{TIER_BADGES[t]} {TIER_TITLES[t]} '
            f'<span style="color:var(--muted);font-weight:400">({len(items)})</span></h2>{cards}</section>'
        )
    if other:  # non-matches (only present when the app's "include non-matches" view is on)
        cards = "".join(_card(r, interactive) for r in other)
        sections.append(
            '<section class="tier" data-tier="other"><h2 class="tier">🗂️ Other roles '
            f'<span style="color:var(--muted);font-weight:400">— not flagged as matches ({len(other)})</span>'
            f"</h2>{cards}</section>"
        )
    body = "".join(sections) or '<p class="m">No in-scope roles yet — run the pipeline.</p>'
    # applications (personal notes) + watchlist health render ONLY in the local app
    apps_html = _applications_section(applications or [], app_notes or {}) if interactive else ""
    watch_html = _watchlist_section(feed or [], funnel or {}) if interactive else ""
    consider = _suggestions_section(suggestions or [], interactive)
    co_opts = "".join(f'<option value="{_attr(c)}">{html.escape(c)}</option>' for c in companies)
    if interactive:
        help_html = ('Click <b>Reject</b> to hide a role, <b>Save</b> to flag it (both teach future '
                     'scoring), or <b>Applied</b> once you\'ve applied — the role moves to the '
                     'Applications tracker above, where you can set its status and leave notes / '
                     'to-dos. <b>Approve</b>/<b>Dismiss</b> companies below — changes save instantly.')
        scripts = f"<script>{_JS}</script><script>{_ACTIONS_JS}</script>"
    else:
        help_html = ('This published page is read-only. Open the local app (<code>job-agent serve</code>) '
                     'for one-click Reject / Save / Approve / Draft buttons.')
        scripts = f"<script>{_JS}</script>"
    nonmatch_toggle = (f'<label><input type="checkbox" id="f-all"{" checked" if include_all else ""}> '
                       "Include non-matches</label>" if interactive else "")

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
{nonmatch_toggle}
</div>
<div id="count"></div>
<p class="help">{help_html}</p>
{watch_html}
{apps_html}
<main>{body}</main>
{consider}
<footer>Generated by job-agent — grounded on real scored listings. Tiers: Strong ≥ {config.TIER_STRONG_MIN}, Worth a look {config.TIER_LOOK_MIN}–{config.TIER_STRONG_MIN - 1}. Click a row to expand.</footer>
{scripts}
</div></body></html>
"""
    return page, stats


def build_site(conn: sqlite3.Connection, *, generated_at: Optional[datetime] = None,
               min_score: Optional[int] = None) -> Tuple[Path, dict, List[sqlite3.Row]]:
    rows = select_master(conn, min_score)
    suggestions = store.list_suggestions(conn, "proposed")
    page, stats = render_html(rows, generated_at=generated_at, suggestions=suggestions)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")
    return SITE_DIR / "index.html", stats, rows


def mark_published(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> None:
    for r in rows:
        store.mark_fingerprint_notified(conn, r["fingerprint"], "site")
