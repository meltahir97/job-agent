"""Ranked Markdown digest generator.

Reads deep-scored jobs straight from SQLite and renders a ranked digest to
./digests/. Pure presentation over stored data — no model calls, nothing
invented. Skips 'skip'-labelled and dismissed roles; orders by fit score.

`only_unnotified` (used from milestone 6) restricts the digest to jobs that have
never been included in a prior digest, so reruns never re-notify.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, store

# Latest deep score per job, joined with feedback so dismissed roles drop out.
_SELECT = """
SELECT j.id, j.fingerprint, j.title, j.company, j.location, j.remote,
       j.salary_min, j.salary_max, j.salary_currency, j.posted_at, j.url,
       s.fit_score, s.label, s.rationale, s.red_flags, s.model
FROM jobs j
JOIN scores s ON s.id = (
    SELECT id FROM scores s2
    WHERE s2.job_id = j.id AND s2.stage = 'deep'
    ORDER BY s2.scored_at DESC, s2.id DESC LIMIT 1
)
LEFT JOIN feedback f ON f.job_id = j.id
WHERE s.label != 'skip'
  AND COALESCE(s.fit_score, 0) >= :min_score
  AND (f.decision IS NULL OR f.decision != 'dismissed')
  {notif_clause}
ORDER BY s.fit_score DESC, j.first_seen_at DESC
"""


def select_for_digest(
    conn: sqlite3.Connection,
    *,
    min_score: int = 60,
    only_unnotified: bool = True,
    limit: Optional[int] = None,
) -> List[sqlite3.Row]:
    """Qualifying rows, deduped by fingerprint (highest score wins per role)."""
    notif = "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.job_id = j.id)" if only_unnotified else ""
    sql = _SELECT.format(notif_clause=notif)
    rows = conn.execute(sql, {"min_score": min_score}).fetchall()
    seen, out = set(), []
    for r in rows:  # rows are score-desc, so the first per fingerprint is the best
        fp = r["fingerprint"]
        if fp in seen:
            continue
        seen.add(fp)
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def _money(v) -> Optional[str]:
    if v is None:
        return None
    v = float(v)
    return f"${v/1000:.0f}k" if v >= 1000 else f"${v:.0f}"


def _salary(row: sqlite3.Row) -> Optional[str]:
    lo, hi = _money(row["salary_min"]), _money(row["salary_max"])
    cur = row["salary_currency"] or ""
    if lo and hi:
        return f"{lo}–{hi} {cur}".strip()
    if lo:
        return f"from {lo} {cur}".strip()
    if hi:
        return f"up to {hi} {cur}".strip()
    return None


def _red_flags(raw) -> List[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        s = str(it).strip()
        if s and s.lower() not in ("none", "n/a", "no red flags", "no concerns"):
            out.append(s)
    return out


def _render_row(row: sqlite3.Row, idx: int) -> str:
    meta = []
    if row["location"]:
        meta.append(row["location"])
    if row["remote"]:
        meta.append("Remote")
    sal = _salary(row)
    if sal:
        meta.append(sal)
    if row["posted_at"]:
        meta.append(f"posted {str(row['posted_at'])[:10]}")
    meta.append(f"id {row['id']}")
    meta_line = "  ·  ".join(meta)

    lines = [
        f"### {idx}. {row['title'] or 'Untitled role'} — {row['company'] or 'Unknown company'}  ·  **{row['fit_score']}/100**",
    ]
    if meta_line:
        lines.append(meta_line)
    if row["rationale"]:
        lines.append(f"\n**Why:** {row['rationale']}")
    flags = _red_flags(row["red_flags"])
    if flags:
        lines.append(f"**Red flags:** {'; '.join(flags)}")
    if row["url"]:
        lines.append(f"\n[View posting →]({row['url']})")
    return "\n".join(lines)


def render_markdown(rows: List[sqlite3.Row], *, generated_at: Optional[datetime] = None) -> str:
    generated_at = generated_at or datetime.now().astimezone()
    matches = [r for r in rows if r["label"] == "match"]
    stretch = [r for r in rows if r["label"] == "stretch"]

    out = [
        f"# Job digest — {generated_at:%Y-%m-%d}",
        "",
        f"_{len(rows)} role(s) · ranked by fit · generated {generated_at:%Y-%m-%d %H:%M} by job-agent_",
        "",
        "_Tune future runs: `job-agent feedback <id> --saved` / `--dismissed`._",
    ]
    if matches:
        out += ["", f"## ⭐ Matches ({len(matches)})", ""]
        for i, r in enumerate(matches, 1):
            out += [_render_row(r, i), "", "---", ""]
    if stretch:
        out += ["", f"## 🔭 Stretch ({len(stretch)})", ""]
        for i, r in enumerate(stretch, 1):
            out += [_render_row(r, i), "", "---", ""]
    if not rows:
        out += ["", "_No qualifying roles in this run._"]
    return "\n".join(out).rstrip() + "\n"


def write_digest(
    conn: sqlite3.Connection,
    *,
    min_score: int = 60,
    only_unnotified: bool = True,
    limit: Optional[int] = None,
    generated_at: Optional[datetime] = None,
) -> Tuple[Optional[Path], int, List[sqlite3.Row]]:
    """Render and write a digest, then record seen-state. Returns (path|None, count, rows).

    Writes nothing if there is nothing new. After writing, every included role and
    its fingerprint duplicates are marked notified so reruns never re-notify.
    """
    rows = select_for_digest(conn, min_score=min_score, only_unnotified=only_unnotified, limit=limit)
    if not rows:
        return None, 0, []
    generated_at = generated_at or datetime.now().astimezone()
    config.DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DIGEST_DIR / f"digest-{generated_at:%Y-%m-%d-%H%M}.md"
    path.write_text(render_markdown(rows, generated_at=generated_at), encoding="utf-8")
    for r in rows:
        store.mark_fingerprint_notified(conn, r["fingerprint"], str(path))
    return path, len(rows), rows
