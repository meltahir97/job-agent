# Job Discovery Agent

A personal, incremental job-discovery agent. It fetches real listings, scores how
well each fits *your* background using Claude, and writes you a ranked Markdown
digest — never re-notifying you about a job you've already seen.

It is built in two **strictly separated** layers:

| Layer | Code | LLM? | Job |
|-------|------|------|-----|
| **Data** | `job_agent/sources/*`, `job_agent/db.py` | No | Fetch, normalize, and store raw listings in SQLite |
| **Reasoning** | `job_agent/reasoning/*` (via `claude-agent-sdk`) | Yes | Triage, deep-score, dedupe, and explain — **only** over records the data layer fetched |

**Grounding guarantee:** the model scores only listings passed to it as data. It
never invents a job, URL, or salary. Missing fields stay `null`.

---

## Pipeline

```
fetch -> normalize -> dedupe -> triage (haiku) -> deep-score (sonnet) -> digest -> feedback
```

Everything is persisted to SQLite, so reruns are **incremental**: already-seen jobs
are skipped and you are never notified twice.

---

## Requirements

- **Python 3.11+** (the `claude-agent-sdk` reasoning layer requires ≥ 3.10).
  This repo's data layer is also 3.9-compatible, but use 3.11+ for the full pipeline.
- **Node.js 18+** and the **Claude Code CLI** (`@anthropic-ai/claude-code`), which the
  Agent SDK uses as its transport. _(Needed from milestone 3 onward.)_
- An **Anthropic API key** and free **Adzuna API** keys.

### Recommended setup with `uv` (no sudo, no system Python changes)

```bash
# 1. Install uv (user-local)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Create a 3.12 virtualenv and install the project
uv venv --python 3.12
uv pip install -e .

# 3. Install the Agent SDK transport (Node CLI), needed from milestone 3 on
npm install -g @anthropic-ai/claude-code
```

> Prefer stock tooling? `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`

### Configure secrets

```bash
cp .env.example .env
# then edit .env and fill in ANTHROPIC_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
```

Secrets live in `.env` only and `.env` is git-ignored.

---

## Usage

```bash
# Initialize the SQLite database + schema (idempotent)
uv run python -m job_agent db init          # or: job-agent db init

# Fetch + store raw listings from Adzuna
job-agent fetch                              # default query set (your roles, Bay Area + remote)
job-agent fetch --what "director strategy" --where "San Francisco" --max 50 --days 30

# Parse your resume into a cached profile (re-parses only when it changes)
job-agent profile
job-agent profile --force                    # force re-parse

# Triage (haiku) + deep-score (sonnet) new jobs; add --opus for opus deep-scoring
job-agent score
job-agent score --opus

# Write a ranked Markdown digest to ./digests (milestone 5)
job-agent digest

# Mark a job saved/dismissed (feeds scoring)  (milestone 7)
job-agent feedback <job_id> --saved|--dismissed

# Run the whole pipeline end-to-end
job-agent run
```

Digests are written to `./digests/` as dated Markdown files.

---

## Scheduling (cron)

Run the full pipeline every morning at 8am:

```cron
0 8 * * *  cd "/path/to/Job Search" && /path/to/.venv/bin/python -m job_agent run >> data/run.log 2>&1
```

_(Filled out once `run` is complete.)_

---

## Project layout

```
job_agent/
  config.py         paths, secrets, model names, search defaults
  db.py             SQLite connection + schema init
  schema.sql        DDL: jobs, scores, feedback, notifications, meta
  models.py         Job dataclass (+ dedup fingerprint)
  sources/
    base.py         JobSource interface + JobQuery (extension point)
    adzuna.py       AdzunaSource                      (milestone 2)
  reasoning/        profile extraction + scoring       (milestones 3-4)
  digest.py         ranked Markdown digest             (milestone 5)
  cli.py            command-line entrypoint
```

---

## Status / roadmap

- [x] **1. Scaffold** — project, env, SQLite schema, CLI surface
- [x] **2. Data layer + AdzunaSource** — `fetch` stores real listings _(offline-tested; live API run pending your Adzuna keys)_
- [x] **3. Resume → cached profile** — pypdf extract + cached JSON, rebuilt only on file change/`--force` _(live LLM call pending API credits)_
- [x] **4. Triage + deep-scoring → DB** — haiku triage then sonnet/opus deep-score; batched, incremental, grounded _(live calls pending API credits)_
- [ ] **5. Markdown digest**
- [ ] **6. Dedup + seen-state (incremental reruns)**
- [ ] **7. Feedback capture wired into scoring**

**Natural next extensions:** email notifications; additional sources (Greenhouse /
Lever / Workable ATS feeds, a paid aggregator); a small web UI for feedback.
