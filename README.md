# Job Discovery Agent

A personal, incremental job-discovery agent. By default it polls the public ATS job
boards of a **watchlist of target companies** (`companies.yaml`), keeps only roles
that are **Bay-Area in-office or US/SF-remote**, scores how well each fits *your*
background using Claude, and writes you a ranked Markdown digest **grouped by
company** — never re-notifying you about a role you've already seen. (A broad Adzuna
keyword search is still available behind `--adzuna`.)

It is built in two **strictly separated** layers:

| Layer | Code | LLM? | Job |
|-------|------|------|-----|
| **Data** | `job_agent/sources/*`, `job_agent/db.py` | No | Poll company ATS feeds (Greenhouse/Lever/Ashby/Workable), normalize, location-filter, store in SQLite |
| **Reasoning** | `job_agent/reasoning/*` (via `claude-agent-sdk`) | Yes | Triage, deep-score, dedupe, and explain — **only** over records the data layer fetched |

**Grounding guarantee:** the model scores only listings passed to it as data. It
never invents a job, URL, salary, company, or ATS feed. Missing fields stay `null`;
companies that can't be confidently resolved are reported, not guessed.

---

## Pipeline

```
watchlist (ATS feeds) -> normalize -> location-filter -> dedupe
   -> triage (haiku) -> deep-score (sonnet) -> digest (grouped by company) -> feedback
```

Everything is persisted to SQLite, so reruns are **incremental**: already-seen roles
are skipped and you are never notified twice.

---

## Requirements

- **Python 3.11+** (the `claude-agent-sdk` reasoning layer requires ≥ 3.10).
  This repo's data layer is also 3.9-compatible, but use 3.11+ for the full pipeline.
- **Node.js 18+** available on PATH. The Agent SDK ships a **bundled** Claude Code CLI
  (its transport), so no separate `@anthropic-ai/claude-code` install is needed.
- An **Anthropic API key** (with credit balance). **Adzuna keys are optional** —
  only needed for the `--adzuna` keyword source; the default watchlist needs no keys.

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

### Configure secrets + watchlist

```bash
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY is required; ADZUNA_* only matter for --adzuna.

# then edit your target companies:
$EDITOR companies.yaml
```

Secrets live in `.env` only (git-ignored). Your watchlist lives in `companies.yaml`
(schema + resolving below).

---

## Usage

```bash
# Initialize the SQLite database + schema (idempotent)
uv run python -m job_agent db init          # or: job-agent db init

# Fetch + store roles from your company watchlist (the default), location-filtered
job-agent fetch
job-agent fetch --adzuna                     # optional: broad Adzuna keyword search instead

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

Digests are written to `./digests/` as timestamped Markdown files, **grouped by
company** and ranked by fit within each company. Each run is incremental:
already-sent roles are skipped, dismissed roles are hidden, and your saved/dismissed
history is fed into future deep-scoring.

---

## Watchlist (companies.yaml)

Your target companies live in `companies.yaml`:

```yaml
companies:
  - name: Stripe
    ats: greenhouse      # greenhouse | lever | ashby | workable | auto
    slug: stripe         # board token; required UNLESS ats: auto
  - name: Notion
    ats: auto            # resolver discovers the board from public ATS URLs
```

- **Explicit** (`ats` + `slug`) is exact and fast. The `slug` is the token in the
  board URL: `boards.greenhouse.io/SLUG`, `jobs.lever.co/SLUG`,
  `jobs.ashbyhq.com/SLUG`, `SLUG.workable.com`.
- **`ats: auto`** guesses the slug from the name and probes the public Greenhouse /
  Lever / Ashby / Workable endpoints; the first board that actually responds wins.
- **Unresolved** companies (auto with no match) are **reported, never guessed** — the
  `fetch`/`run` output lists them under `UNRESOLVED` so you can add the right
  `ats` + `slug` manually. The agent never fabricates a feed or a listing.

Each board is fetched, normalized into the standard schema, **location-filtered**, and
deduped/seen-tracked exactly like every other source.

### Tuning the location filter

A role is **kept** if it's in the Bay Area **or** remote-inclusive of the US/CA, and
**dropped** only when clearly elsewhere-only; genuinely ambiguous locations are kept
with `remote = null` (never guessed). All the place lists are in **one spot** —
`job_agent/config.py`, the `# Watchlist location filter (TUNE HERE)` block
(`BAY_AREA_TERMS`, `REMOTE_TERMS`, `US_TERMS`, `NON_US_TERMS`, `US_NON_BAY_TERMS`, …).
Add or remove cities/terms there to widen or tighten the net.

> Tip: if your watchlist is private, add `companies.yaml` to `.gitignore`.

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
  config.py         paths, secrets, model names, search + location-filter config
  db.py             SQLite connection + schema init
  schema.sql        DDL: jobs, scores, feedback, notifications, meta
  models.py         Job dataclass (+ dedup fingerprint)
  store.py          job upsert/dedup, scoring writes, seen-state, feedback
  companies.py      companies.yaml loader (watchlist schema)
  queries.py        Adzuna default query set (optional --adzuna source)
  textutil.py       HTML->text + epoch->ISO helpers
  sources/
    base.py         JobSource interface + JobQuery (extension point)
    ats.py          public ATS HTTP layer (endpoints, raw fetch, probe)
    resolver.py     ats=auto board resolver + unresolved reporting
    ats_sources.py  Greenhouse / Lever / Ashby / Workable sources
    location.py     Bay-Area / US-remote location filter
    watchlist.py    WatchlistSource orchestration (default source)
    adzuna.py       AdzunaSource (optional, behind --adzuna)
  reasoning/
    llm.py          single grounded, tool-free seam to claude-agent-sdk
    profile.py      resume -> cached structured profile
    scoring.py      triage (haiku) + deep score (sonnet/opus)
  digest.py         company-grouped Markdown digest + seen-state
  cli.py            command-line entrypoint
```

---

## Status / roadmap

### Original build (broad Adzuna keyword search) — complete & live-verified

- [x] **1. Scaffold** — project, env, SQLite schema, CLI surface
- [x] **2. Data layer + AdzunaSource** — `fetch` stores real listings (live-verified against Adzuna)
- [x] **3. Resume → cached profile** — pypdf extract + cached JSON, rebuilt only on file change/`--force` _(live LLM call pending API credits)_
- [x] **4. Triage + deep-scoring → DB** — haiku triage then sonnet/opus deep-score; batched, incremental, grounded _(live calls pending API credits)_
- [x] **5. Markdown digest** — ranked Matches/Stretch with rationale, red flags, salary, links (verified on real listings)
- [x] **6. Dedup + seen-state** — fingerprint dedup + never re-notify (verified: collapses duplicate listings)
- [x] **7. Feedback capture wired into scoring** — `feedback` upserts saved/dismissed; dismissed hidden, history fed into the deep prompt

### Watchlist pivot (current strategy)

- [x] **1. companies.yaml** schema + loader + `ats=auto` resolver + unresolved reporting
- [x] **2. ATS sources** — Greenhouse / Lever / Ashby / Workable behind `JobSource`
- [x] **3. WatchlistSource** orchestration + tunable location filter
- [x] **4. Wired as default** — watchlist is the default `fetch`/`run`; Adzuna behind `--adzuna`; digest grouped by company
- [ ] **5. Live verification** — one real feed per ATS + full pipeline on the real watchlist

The pipeline is **code-complete with 51 offline tests**, all green. The original
Adzuna build and the reasoning layer are live-verified; watchlist live-verification
is step 5.

**Deferred to later phases (not built yet):** resume tailoring, cover letters,
application status tracking, inbox monitoring. **Near-term extensions:** email
delivery of the digest; a paid aggregator source; a small web UI for feedback.

---

## Troubleshooting

- **`Credit balance is too low`** during `profile`/`score`/`run` → add credit at
  https://console.anthropic.com/settings/billing. The key/transport are fine; the
  account just has no balance.
- **A company shows `UNRESOLVED`** → auto-resolution found no public board. Set `ats`
  + `slug` explicitly in `companies.yaml` (slug = the token in the board URL).
- **`Adzuna credentials missing`** → only affects `--adzuna`; fill `ADZUNA_APP_ID` /
  `ADZUNA_APP_KEY` in `.env`.
- **Cost control** → triage uses cheap haiku and only un-triaged jobs; deep scoring
  only runs on triage survivors, batched. Use `--max`/`--days` on `fetch` to bound
  volume and `--min-score` on `digest` to tighten the bar.
