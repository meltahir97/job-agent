"""Domain-level persistence: writing/reading Job records (stdlib only).

`db.py` owns the connection + schema; this module owns job upserts and the
dedup/seen bookkeeping that the pipeline relies on.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple

from .models import Job


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b(v: Optional[bool]) -> Optional[int]:
    return None if v is None else (1 if v else 0)


def upsert_job(conn: sqlite3.Connection, job: Job) -> Tuple[int, bool]:
    """Insert a job, or update it in place if (source, source_job_id) already exists.

    Returns (job_id, is_new). On update we refresh fields + last_seen_at but keep
    the original first_seen_at, so reruns are incremental and idempotent.
    """
    now = now_iso()
    raw_json = json.dumps(job.raw, ensure_ascii=False, sort_keys=True)

    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
        (job.source, job.source_job_id),
    ).fetchone()

    fields = (
        job.fingerprint, job.title, job.company, job.location, _b(job.remote),
        job.description, job.url, job.salary_min, job.salary_max, job.salary_currency,
        job.category, job.contract_type, job.posted_at, raw_json,
    )

    if row:
        job_id = row["id"]
        conn.execute(
            """UPDATE jobs SET
                 fingerprint=?, title=?, company=?, location=?, remote=?,
                 description=?, url=?, salary_min=?, salary_max=?, salary_currency=?,
                 category=?, contract_type=?, posted_at=?, raw_json=?, last_seen_at=?
               WHERE id=?""",
            (*fields, now, job_id),
        )
        conn.commit()
        return job_id, False

    cur = conn.execute(
        """INSERT INTO jobs (
             source, source_job_id, fingerprint, title, company, location, remote,
             description, url, salary_min, salary_max, salary_currency, category,
             contract_type, posted_at, raw_json, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.source, job.source_job_id, *fields, now, now),
    )
    conn.commit()
    return int(cur.lastrowid), True


def count_jobs(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]


# --- scoring persistence + SQL pre-filters (cost control) -------------------

def record_score(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    stage: str,                 # 'triage' | 'deep'
    model: str,
    keep: Optional[bool] = None,
    fit_score: Optional[int] = None,
    label: Optional[str] = None,
    rationale: Optional[str] = None,
    red_flags=None,             # list -> stored as JSON text
) -> None:
    conn.execute(
        "INSERT INTO scores (job_id, stage, keep, fit_score, label, rationale, red_flags, model, scored_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            stage,
            None if keep is None else int(bool(keep)),
            None if fit_score is None else int(fit_score),
            label,
            rationale,
            None if red_flags is None else json.dumps(red_flags, ensure_ascii=False),
            model,
            now_iso(),
        ),
    )
    conn.commit()


def jobs_needing_triage(conn: sqlite3.Connection):
    """Jobs that have never been triaged (so we never re-pay to triage)."""
    return conn.execute(
        "SELECT * FROM jobs j "
        "WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_id=j.id AND s.stage='triage') "
        "ORDER BY j.first_seen_at DESC"
    ).fetchall()


def jobs_needing_deep(conn: sqlite3.Connection):
    """Triage survivors (keep=1) that have not yet been deep-scored."""
    return conn.execute(
        "SELECT j.* FROM jobs j "
        "JOIN scores t ON t.job_id=j.id AND t.stage='triage' AND t.keep=1 "
        "WHERE NOT EXISTS (SELECT 1 FROM scores d WHERE d.job_id=j.id AND d.stage='deep') "
        "GROUP BY j.id ORDER BY j.first_seen_at DESC"
    ).fetchall()


def feedback_examples(conn: sqlite3.Connection, limit: int = 20):
    """Recent saved/dismissed decisions to feed back into deep scoring."""
    return conn.execute(
        "SELECT f.decision, j.title, j.company FROM feedback f "
        "JOIN jobs j ON j.id=f.job_id ORDER BY f.updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


# --- seen-state for notifications (incremental reruns) ----------------------

def mark_fingerprint_notified(conn: sqlite3.Connection, fingerprint: str, digest_path: str) -> None:
    """Mark every job sharing this fingerprint as notified, so neither the role
    nor its duplicates are ever sent again."""
    conn.execute(
        "INSERT OR IGNORE INTO notifications (job_id, digest_path, notified_at) "
        "SELECT id, ?, ? FROM jobs WHERE fingerprint = ?",
        (digest_path, now_iso(), fingerprint),
    )
    conn.commit()
