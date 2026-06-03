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
- **Node.js 18+** available on PATH. The Agent SDK ships a **bundled** Claude Code CLI
  (its transport), so no separate `@anthropic-ai/claude-code` install is needed.
- An **Anthropic API key** (with credit balance) and free **Adzuna API** keys.

### Recommended setup with `uv` (no sudo, no system Python changes)

```bash
# 1. Install uv (user-local)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Create a 3.12 virtualenv and install the project
uv venv --python 3.12
uv pip install -e .
# (the Agent SDK bundles its Claude Code CLI transport — no extra install needed)
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

# Write a ranked Markdown digest to ./digests/
job-agent digest                             # default: fit >= 60, only NEW roles
job-agent digest --min-score 75 --limit 25
job-agent digest --all                       # re-include roles already sent

# Mark a job saved/dismissed (feeds future scoring); ids are shown in the digest
job-agent feedback 42 --saved
job-agent feedback 42 --dismissed --note "too junior"
job-agent feedback --list

# Run the whole pipeline end-to-end (fetch -> profile -> triage -> deep-score -> digest)
job-agent run
```

Digests are written to `./digests/` as timestamped Markdown files. Each run is
incremental: already-sent roles are skipped, dismissed roles are hidden, and your
saved/dismissed history is fed into future deep-scoring.

---

## Scheduling (cron)

Run the full pipeline every morning at 8am:

```cron
0 8 * * *  cd "/Users/muhammadeltahir/Projects/Job Search" && ./.venv/bin/python -m job_agent run >> data/run.log 2>&1
```

`run` is fully wired (fetch → profile → triage → deep-score → digest) and
incremental, so a daily cron only ever scores/notifies genuinely new roles.

---

## Project layout

```
job_agent/
  config.py         paths, secrets, model names, search defaults
  db.py             SQLite connection + schema init
  schema.sql        DDL: jobs, scores, feedback, notifications, meta
  models.py         Job dataclass (+ dedup fingerprint)
  store.py          job upsert/dedup, scoring writes, seen-state, feedback
  queries.py        default search query set (your roles × Bay Area/Remote)
  sources/
    base.py         JobSource interface + JobQuery (extension point)
    adzuna.py       AdzunaSource
  reasoning/
    llm.py          single grounded, tool-free seam to claude-agent-sdk
    profile.py      resume -> cached structured profile
    scoring.py      triage (haiku) + deep score (sonnet/opus)
  digest.py         ranked Markdown digest + seen-state
  cli.py            command-line entrypoint
```

---

## Status / roadmap

- [x] **1. Scaffold** — project, env, SQLite schema, CLI surface
- [x] **2. Data layer + AdzunaSource** — `fetch` stores real listings (live-verified against Adzuna)
- [x] **3. Resume → cached profile** — pypdf extract + cached JSON, rebuilt only on file change/`--force` _(live LLM call pending API credits)_
- [x] **4. Triage + deep-scoring → DB** — haiku triage then sonnet/opus deep-score; batched, incremental, grounded _(live calls pending API credits)_
- [x] **5. Markdown digest** — ranked Matches/Stretch with rationale, red flags, salary, links (verified on real listings)
- [x] **6. Dedup + seen-state** — fingerprint dedup + never re-notify (verified: collapses duplicate listings)
- [x] **7. Feedback capture wired into scoring** — `feedback` upserts saved/dismissed; dismissed hidden, history fed into the deep prompt

The pipeline is **code-complete with 22 offline tests**. The reasoning steps
(profile, score) need Anthropic API credit to run live — everything else is
verified end-to-end.

**Natural next extensions:** email notifications (SMTP/SES on the digest);
additional sources (Greenhouse / Lever / Workable ATS feeds, a paid aggregator);
a small web UI for feedback.

---

## Troubleshooting

- **`Credit balance is too low`** during `profile`/`score`/`run` → add credit at
  https://console.anthropic.com/settings/billing. The key/transport are fine; the
  account just has no balance.
- **`Adzuna credentials missing`** → fill `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` in `.env`.
- **Cost control** → triage uses cheap haiku and only un-triaged jobs; deep scoring
  only runs on triage survivors, batched. Use `--max`/`--days` on `fetch` to bound
  volume and `--min-score` on `digest` to tighten the bar.
