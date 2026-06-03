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
