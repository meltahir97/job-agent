"""SQLite access: connection, schema init, and small helpers.

Pure stdlib so the data layer has no third-party dependency.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = "1"


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table first shipped (CREATE TABLE IF NOT EXISTS
    won't alter an existing table). Each ADD COLUMN is a no-op if already present."""
    additions = {
        "drafts": [
            "ALTER TABLE drafts ADD COLUMN drive_url TEXT",
            "ALTER TABLE drafts ADD COLUMN resume_url TEXT",
            "ALTER TABLE drafts ADD COLUMN cover_url TEXT",
        ],
    }
    for stmts in additions.values():
        for sql in stmts:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
