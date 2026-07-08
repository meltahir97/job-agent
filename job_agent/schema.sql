-- Job Discovery Agent — SQLite schema
-- All timestamps are ISO-8601 strings (UTC). Missing source fields stay NULL;
-- the data/reasoning layers must never fabricate values.

-- Key/value metadata: schema_version, resume_hash, profile_path, last_run_at, ...
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Normalized job listings fetched by the data layer.
-- raw_json preserves the exact source payload for grounding/provenance.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,            -- e.g. 'adzuna'
    source_job_id   TEXT    NOT NULL,            -- id within that source
    fingerprint     TEXT    NOT NULL,            -- normalized hash for cross-source dedup
    title           TEXT,
    company         TEXT,
    location        TEXT,
    remote          INTEGER,                     -- 1 / 0 / NULL
    description     TEXT,
    url             TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    category        TEXT,
    contract_type   TEXT,
    posted_at       TEXT,                        -- ISO-8601 from source, may be NULL
    raw_json        TEXT    NOT NULL,            -- original payload (provenance)
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    UNIQUE (source, source_job_id)               -- idempotent re-fetch
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs (fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_source      ON jobs (source);

-- Model scoring output. One row per (job, stage) scoring event; history kept.
-- stage = 'triage' (cheap keep/drop) or 'deep' (full 0-100 fit).
CREATE TABLE IF NOT EXISTS scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    stage       TEXT    NOT NULL,                -- 'triage' | 'deep'
    keep        INTEGER,                         -- triage: 1 keep / 0 drop
    fit_score   INTEGER,                         -- deep: 0-100
    label       TEXT,                            -- deep: 'match' | 'stretch' | 'skip'
    rationale   TEXT,
    red_flags   TEXT,                            -- JSON array (text)
    model       TEXT    NOT NULL,
    scored_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_job   ON scores (job_id);
CREATE INDEX IF NOT EXISTS idx_scores_stage ON scores (stage);

-- User feedback that feeds back into future scoring. One current decision per job.
CREATE TABLE IF NOT EXISTS feedback (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    decision   TEXT NOT NULL,                    -- 'saved' | 'dismissed'
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Seen-state for notifications so reruns never re-notify about the same job.
-- A job present here has already appeared in a digest and is skipped thereafter.
CREATE TABLE IF NOT EXISTS notifications (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    digest_path TEXT,
    notified_at TEXT NOT NULL
);

-- Generated application drafts (resume + cover letter) per role. LOCAL ONLY —
-- never published to the website. One current set per job; --regenerate replaces.
CREATE TABLE IF NOT EXISTS drafts (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    company     TEXT,
    title       TEXT,
    dir         TEXT,                            -- local dir (fallback) OR Drive folder link
    resume_md   TEXT,
    resume_docx TEXT,
    cover_md    TEXT,
    cover_docx  TEXT,
    drive_url   TEXT,                            -- Drive folder webViewLink (primary output)
    resume_url  TEXT,                            -- Drive Google-Doc link (resume)
    cover_url   TEXT,                            -- Drive Google-Doc link (cover letter)
    model       TEXT,
    created_at  TEXT NOT NULL
);

-- Weekly company-discovery proposals (PROPOSE-ONLY; never auto-added to the
-- watchlist). norm_name suppresses re-proposing the same company.
CREATE TABLE IF NOT EXISTS suggestions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    norm_name    TEXT NOT NULL UNIQUE,           -- lowercased key, dedup / suppression
    reason       TEXT,                           -- why-it-fits, grounded in evidence
    evidence_url TEXT,                           -- source URL we can cite
    ats          TEXT,                           -- resolved ATS, if any
    slug         TEXT,                           -- resolved board slug, if any
    status       TEXT NOT NULL,                  -- proposed | approved | dismissed | unverified
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Application tracking: a row appears when the user marks a role as applied
-- (via the local app's "Applied" button). One row per job; status advances.
CREATE TABLE IF NOT EXISTS applications (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'applied', -- applied | interviewing | offer | rejected | withdrawn
    applied_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Free-form updates, notes, and to-dos the user leaves on an application.
-- kind='todo' rows carry done 0/1; kind='note' rows leave done NULL.
CREATE TABLE IF NOT EXISTS app_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'note',    -- 'note' | 'todo'
    text       TEXT NOT NULL,
    done       INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_notes_job ON app_notes (job_id);

-- Watchlist feed health, refreshed every pipeline run: is each tracked company's
-- board actually reachable, and how many roles came through last fetch?
CREATE TABLE IF NOT EXISTS feed_status (
    company    TEXT PRIMARY KEY,
    ats        TEXT,
    slug       TEXT,
    ok         INTEGER NOT NULL DEFAULT 0,
    fetched    INTEGER NOT NULL DEFAULT 0,             -- roles the feed returned
    kept       INTEGER NOT NULL DEFAULT 0,             -- survived the location filter
    error      TEXT,                                   -- why it's broken / unresolved
    checked_at TEXT NOT NULL
);
