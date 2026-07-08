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


# --- feedback (saved/dismissed -> future scoring) ---------------------------

def get_job(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def record_feedback(conn: sqlite3.Connection, job_id: int, decision: str, note: Optional[str] = None) -> None:
    """Upsert the candidate's current decision ('saved' | 'dismissed') for a job."""
    now = now_iso()
    conn.execute(
        "INSERT INTO feedback (job_id, decision, note, created_at, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET decision=excluded.decision, note=excluded.note, updated_at=excluded.updated_at",
        (job_id, decision, note, now, now),
    )
    conn.commit()


def list_feedback(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT f.job_id, f.decision, f.note, j.title, j.company FROM feedback f "
        "JOIN jobs j ON j.id=f.job_id ORDER BY f.updated_at DESC"
    ).fetchall()


def decided_job_ids(conn: sqlite3.Connection) -> set:
    """Job ids that already have a saved/dismissed decision (skip them in review)."""
    return {r["job_id"] for r in conn.execute("SELECT job_id FROM feedback")}


def clear_feedback(conn: sqlite3.Connection, job_id: int) -> None:
    """Remove a saved/dismissed decision (undo)."""
    conn.execute("DELETE FROM feedback WHERE job_id = ?", (job_id,))
    conn.commit()


# --- application drafts (resume + cover letter, local-only) ------------------

def record_draft(conn: sqlite3.Connection, job_id: int, *, company, title, dir,
                 resume_md=None, resume_docx=None, cover_md=None, cover_docx=None,
                 drive_url=None, resume_url=None, cover_url=None, model=None) -> None:
    conn.execute(
        "INSERT INTO drafts (job_id, company, title, dir, resume_md, resume_docx, cover_md, cover_docx, "
        "drive_url, resume_url, cover_url, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET company=excluded.company, title=excluded.title, dir=excluded.dir, "
        "resume_md=excluded.resume_md, resume_docx=excluded.resume_docx, cover_md=excluded.cover_md, "
        "cover_docx=excluded.cover_docx, drive_url=excluded.drive_url, resume_url=excluded.resume_url, "
        "cover_url=excluded.cover_url, model=excluded.model, created_at=excluded.created_at",
        (job_id, company, title, (str(dir) if dir else None),
         (str(resume_md) if resume_md else None), (str(resume_docx) if resume_docx else None),
         (str(cover_md) if cover_md else None), (str(cover_docx) if cover_docx else None),
         drive_url, resume_url, cover_url, model, now_iso()),
    )
    conn.commit()


def get_draft(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM drafts WHERE job_id = ?", (job_id,)).fetchone()


def get_draft_for_role(conn: sqlite3.Connection, company: str, title: str):
    """A draft for the same company+title under ANY job id — catches a role that was
    re-fetched under a new id, so the same posting is never drafted twice."""
    return conn.execute(
        "SELECT * FROM drafts WHERE company = ? AND title = ? ORDER BY created_at DESC LIMIT 1",
        (company, title),
    ).fetchone()


def drafted_job_ids(conn: sqlite3.Connection) -> set:
    return {r["job_id"] for r in conn.execute("SELECT job_id FROM drafts")}


# --- application tracking (applied roles + notes / to-dos) -------------------

APP_STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")


def set_application(conn: sqlite3.Connection, job_id: int, status: str = "applied") -> None:
    """Mark a job as applied (or advance its status). Keeps the original applied_at."""
    now = now_iso()
    conn.execute(
        "INSERT INTO applications (job_id, status, applied_at, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
        (job_id, status, now, now),
    )
    conn.commit()


def get_application(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def clear_application(conn: sqlite3.Connection, job_id: int) -> None:
    """Untrack an application (misclick undo). Its notes are kept."""
    conn.execute("DELETE FROM applications WHERE job_id = ?", (job_id,))
    conn.commit()


def applied_job_ids(conn: sqlite3.Connection) -> set:
    return {r["job_id"] for r in conn.execute("SELECT job_id FROM applications")}


def list_applications(conn: sqlite3.Connection):
    """Applications newest-activity-first, joined with the job + draft links."""
    return conn.execute(
        """SELECT a.job_id AS id, a.status, a.applied_at, a.updated_at,
                  j.title, j.company, j.location, j.url,
                  (SELECT drive_url FROM drafts d WHERE d.job_id = a.job_id) AS draft_url,
                  (SELECT COUNT(*) FROM app_notes n
                    WHERE n.job_id = a.job_id AND n.kind='todo' AND COALESCE(n.done,0)=0) AS open_todos
           FROM applications a JOIN jobs j ON j.id = a.job_id
           ORDER BY a.updated_at DESC""",
    ).fetchall()


def add_app_note(conn: sqlite3.Connection, job_id: int, text: str, kind: str = "note") -> int:
    cur = conn.execute(
        "INSERT INTO app_notes (job_id, kind, text, done, created_at) VALUES (?,?,?,?,?)",
        (job_id, kind, text, 0 if kind == "todo" else None, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_app_notes(conn: sqlite3.Connection, job_id: int):
    return conn.execute(
        "SELECT * FROM app_notes WHERE job_id = ? ORDER BY created_at, id", (job_id,)
    ).fetchall()


def set_note_done(conn: sqlite3.Connection, note_id: int, done: bool) -> None:
    conn.execute("UPDATE app_notes SET done = ? WHERE id = ?", (1 if done else 0, note_id))
    conn.commit()


def delete_app_note(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("DELETE FROM app_notes WHERE id = ?", (note_id,))
    conn.commit()


# --- company-discovery suggestions (propose-only) ---------------------------

def add_suggestion(conn: sqlite3.Connection, *, company, norm_name, reason,
                   evidence_url, ats, slug, status) -> bool:
    """Insert a proposal; no-op if this company was already proposed/dismissed.
    Returns True if a new row was inserted."""
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO suggestions (company, norm_name, reason, evidence_url, ats, slug, status, first_seen, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(norm_name) DO NOTHING",
        (company, norm_name, reason, evidence_url, ats, slug, status, now, now),
    )
    conn.commit()
    return cur.rowcount > 0


def existing_suggestion_names(conn: sqlite3.Connection) -> set:
    return {r["norm_name"] for r in conn.execute("SELECT norm_name FROM suggestions")}


def list_suggestions(conn: sqlite3.Connection, status: Optional[str] = None):
    if status:
        return conn.execute("SELECT * FROM suggestions WHERE status = ? ORDER BY first_seen DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM suggestions ORDER BY first_seen DESC").fetchall()


def get_suggestion(conn: sqlite3.Connection, sid: int):
    return conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()


def set_suggestion_status(conn: sqlite3.Connection, sid: int, status: str) -> None:
    conn.execute("UPDATE suggestions SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), sid))
    conn.commit()
