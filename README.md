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

- **Python 3.11+** (3.12 recommended; install deps with `uv pip install -e .`).
  The reasoning layer calls the **Anthropic Messages API** via the `anthropic` SDK —
  no Node.js / Agent-SDK CLI needed anymore.
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

job-agent score --batch                      # cheaper Batch API (latency-tolerant)

# Publish the website (./docs/index.html) + optional email nudge
job-agent publish
job-agent publish --dry-run                   # build locally + print; don't push/email

# Mark a job saved/dismissed (feeds future scoring); ids are shown in the site/digest
job-agent feedback 42 --saved
job-agent feedback 42 --dismissed --note "too junior"

# Markdown digest (optional secondary output; tiered, NEW-only, grouped by company)
job-agent digest

# Full pipeline: fetch -> score -> publish website (+ email)
job-agent run
job-agent run --dry-run                       # preview the site + email, publish nothing
job-agent run --if-due --force                # scheduled-run 47h guard / override
```

The **website** (`./docs/index.html`, published via GitHub Pages) is the primary
output — see *Publishing* below. The Markdown `digest` is an optional secondary
output. Both are tiered (Strong ≥ 75, Worth-a-look 55–74), grounded on real scored
listings, and incremental (dismissed roles hidden; saved/dismissed history feeds
future scoring).

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

## Publishing (GitHub Pages)

Each run writes a single self-contained `./docs/index.html` (inline CSS, no build
step) — all current in-scope roles, grouped by tier → company, with fit, rationale,
red flags, dates, Apply links, and a **NEW** badge for roles first seen since the
last publish. The site accumulates in SQLite, so it's incremental across runs.

**One-time setup (the agent can't do this without GitHub auth):**

1. Create an empty GitHub repo (e.g. `job-agent`). **Don't** add a README/license.
2. Point this repo at it and push:
   ```bash
   git remote add origin https://github.com/<you>/job-agent.git
   git push -u origin main
   ```
3. Enable Pages: GitHub repo → **Settings → Pages** → **Source: Deploy from a branch**
   → **Branch: `main`**, **Folder: `/docs`** → **Save**.
4. Your site goes live at **`https://<you>.github.io/job-agent/`** (first build ~1 min).
   Put that URL in `.env` as `SITE_URL=` so the email nudge links to it.

After that, `job-agent publish` (or `run`) commits `./docs` and `git push`es — Pages
redeploys automatically. `--dry-run` builds the site locally and prints what *would*
publish/email, pushing/sending nothing.

> If your watchlist/résumé data shouldn't be public, use a **private** repo with
> Pages, or publish to a separate public repo that contains only `docs/`.

### Email nudge (optional)

If `SMTP_USER` + `SMTP_APP_PASSWORD` (a Gmail **app password**) are in `.env`, each
run with ≥1 new role emails a short nudge ("N new — X strong, Y worth a look; top 3;
site link") to `NOTIFY_EMAIL` (defaults to muhammad.e.eltahir@gmail.com). 0 new → no
email; missing creds → skipped silently. Never blocks the run.

---

## Scheduling (every 2 days, launchd — macOS)

`run --if-due` records a last-success timestamp and **skips if <47h since the last
success** (so a wake-from-sleep catch-up never double-runs); `--force` overrides.
`scripts/run_scheduled.sh` wraps it; `scripts/com.jobagent.run.plist` triggers it
every 2 days. **Activate it yourself** (not auto-loaded):

```bash
cp scripts/com.jobagent.run.plist ~/Library/LaunchAgents/
launchctl load   ~/Library/LaunchAgents/com.jobagent.run.plist   # start
launchctl list | grep com.jobagent.run                           # status
tail -f data/run.log                                             # watch a run
launchctl unload ~/Library/LaunchAgents/com.jobagent.run.plist   # stop
```

> launchd only fires while the Mac is **awake**; the 47h guard makes missed runs
> self-correct on the next wake. For true 24/7 cadence, run the same wrapper from an
> always-on host (a small VM / Raspberry Pi) via `cron`, or a CI scheduler.

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
  tiers.py          tier thresholds (Strong / Worth a look) — shared
  website.py        self-contained GitHub Pages site (./docs/index.html)
  notify.py         optional Gmail-SMTP nudge
  sources/
    base.py         JobSource interface + JobQuery (extension point)
    ats.py          public ATS HTTP layer (endpoints, raw fetch, probe)
    resolver.py     ats=auto board resolver + unresolved reporting
    ats_sources.py  Greenhouse / Lever / Ashby / Workable / SmartRecruiters / Workday
    location.py     Bay-Area / US-remote location filter (positive US-signal)
    watchlist.py    WatchlistSource orchestration (default source)
    adzuna.py       AdzunaSource (optional, behind --adzuna)
  reasoning/
    llm.py          Anthropic Messages API seam (concurrent asyncio + caching + Batch)
    profile.py      resume -> cached structured profile
    scoring.py      triage (haiku) + deep score (sonnet/opus)
  digest.py         company/tier Markdown digest + seen-state
  cli.py            command-line entrypoint
scripts/
  run_scheduled.sh        launchd wrapper (run --if-due)
  com.jobagent.run.plist  launchd job (every 2 days)
docs/index.html           the published website (GitHub Pages source)
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
- [x] **5. Live verification** — Greenhouse (Stripe, Databricks), Ashby (Notion, Ramp), Lever (Mistral) return real postings; full pipeline ran live (732 in-scope → triage → deep-score → company-grouped digest)

### Final consolidation (current)

- [x] **M0 — Efficiency:** scoring moved off the Agent SDK subprocess transport to
  the raw **Anthropic Messages API** (asyncio + semaphore 5, SDK retries; optional
  `--batch`). A full ~1.4k-job run went **~35 min → ~4–5 min, ~$1.5–2.5** (`--batch`
  ~50% cheaper). Same prompts/models/JSON contract/grounding.
- [x] **M1 — Location leak fixed:** positive US-signal filter (state names/abbrevs,
  "Remote - US", …). Seoul/Mumbai/Taipei/Bogotá + Santa Monica/Stamford/Cary now drop.
- [x] **M2 — More sources:** added **SmartRecruiters** (validated) + **Workday**.
  Resolved **NVIDIA** (Workday). Still UNRESOLVED (no fabrication): **Apple** (custom
  site; Workday 401), **EA** (not on SmartRecruiters under any tried id; private ATS),
  **Pixar** (no standalone board; lives on Disney's Workday `disney/wd5/disneycareer`),
  **Google/YouTube** (custom), **TCG** (no public board).
- [x] **M3 — Tiers:** Strong ≥ 75 / Worth-a-look 55–74 (config `TIER_*`), below excluded.
- [x] **M4 — Website + email:** GitHub Pages `docs/index.html`, NEW badges, optional
  Gmail nudge, `--dry-run` gate.
- [x] **M5 — Schedule:** `run --if-due` (47h guard) + launchd plist (every 2 days).

**64 offline tests, all green.** Workable still fixture-only (no public board found).
Markdown `digest` retained as a secondary output.

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
