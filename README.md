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
| **Reasoning** | `job_agent/reasoning/*` (Anthropic Messages API) | Yes | Triage, deep-score, profile synthesis, drafting, discovery — **only** over data we pass it |

**Grounding guarantee:** the model scores only listings passed to it as data. It
never invents a job, URL, salary, company, or ATS feed. Missing fields stay `null`;
companies that can't be confidently resolved are reported, not guessed.

---

## Pipeline

```
(Google Drive resumes/cover letters -> MASTER PROFILE + voice profile)
watchlist (ATS feeds) -> normalize -> location-filter -> dedupe
   -> triage (haiku) -> deep-score (sonnet, vs. FULL profile)
   -> discover new companies (web-search + verify, propose-only)
   -> draft tailored resume + cover letter for Strong matches (local)
   -> publish website -> email nudge -> feedback
```

Everything is persisted to SQLite, so reruns are **incremental**: already-seen roles
are skipped and you are never notified twice. The full pipeline is one command
(`job-agent run`); each stage is also runnable on its own.

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
# installs anthropic, requests, pypdf, pyyaml, google-api-python-client, google-auth, python-docx
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

# Build the MASTER PROFILE from ALL your Google Drive resumes/CVs/cover letters
job-agent master-profile                     # requires Drive set up (see below)
job-agent master-profile --force             # rebuild even if the doc set is unchanged

# Triage (haiku) + deep-score (sonnet) new jobs against your FULL background
job-agent score
job-agent score --opus
job-agent score --batch                      # cheaper Batch API (latency-tolerant)

# Generate tailored resume + cover-letter drafts (local only; never published)
job-agent drafts                             # Strong matches by default
job-agent drafts --all --regenerate          # every tier; overwrite existing

# Propose NEW target companies (web-search + verify; propose-only, weekly cadence)
job-agent discover                           # --force to ignore the 7-day guard
job-agent discover --list                    # show open proposals + their ids
job-agent approve 3                          # append proposal #3 to companies.yaml
job-agent dismiss 4                          # suppress proposal #4 from re-proposal

# Triage results (the published page is read-only — act from your terminal)
job-agent review                             # guided: approve/dismiss companies + save/reject jobs
job-agent reject 42                          # hide a sourced role (ids show when you expand a row); teaches scoring
job-agent save 42                            # mark a role interesting; teaches scoring

# Publish the website (./docs/index.html) + optional email nudge
job-agent publish
job-agent publish --dry-run                   # build locally + print; don't push/email
job-agent publish --no-email                  # push the site but don't email

# Local interactive app — the site with working Reject/Save/Approve BUTTONS (no terminal)
job-agent serve                               # opens http://127.0.0.1:8765

# Mark a job saved/dismissed (feeds future scoring); ids are shown in the site/digest
job-agent feedback 42 --saved
job-agent feedback 42 --dismissed --note "too junior"

# Markdown digest (optional secondary output; tiered, NEW-only, grouped by company)
job-agent digest

# Full pipeline: fetch -> master-profile -> score -> discover -> drafts -> publish (+ email)
job-agent run
job-agent run --dry-run                       # preview site + drafts + email, publish/send nothing
job-agent run --if-due --force                # scheduled-run 47h guard / override
job-agent run --no-discover --no-drafts       # skip the new stages for a lean run
```

The **website** (`./docs/index.html`, published via GitHub Pages) is the primary
output — see *Publishing* below. The Markdown `digest` is an optional secondary
output. Both are tiered (Strong ≥ 75, Worth-a-look 30–74), grounded on real scored
listings, and incremental (dismissed roles hidden; saved/dismissed history feeds
future scoring). The website rows are collapsed by default and expand on click, with
client-side filters (tier / company / remote / pay) and a "drafts ready" tag.

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

## Master profile (Google Drive)

Scoring and drafting judge you against your **whole career**, not just your latest
title. `master-profile` reads **every resume / CV / cover letter** you've shared with
a Google service account and synthesizes them into one cached `profile/master_profile.json`
(the union of all employers, roles, skills, achievements, and experience threads) plus
a `voice_profile.json` (tone/phrasing, from the cover letters) used for drafting.

**Grounding:** the profile may contain only facts that appear in your documents —
nothing is invented; conflicting titles/dates are reconciled and noted in `variances`.

**One-time setup:**

1. Create a Google Cloud **service account**, download its JSON key, and point
   `.env` at it: `GOOGLE_SERVICE_ACCOUNT_JSON=/abs/path/to/key.json` (git-ignored).
2. **Enable the Drive API** for that project (Cloud Console → APIs & Services → enable
   "Google Drive API").
3. **Share** your resume/cover-letter folder (Viewer) with the service-account email
   (e.g. `…@<project>.iam.gserviceaccount.com`).
4. `job-agent master-profile` — it lists the files it found and summarizes the profile.

If nothing is shared (or the API is off) the agent **stops and tells you** what to
fix; it never silently proceeds. Scoring falls back to the resume profile when the
master profile isn't available.

## Application drafts (resume + cover letter)

`job-agent drafts` generates a **tailored resume and cover letter** for each Strong
match (`--all` for every tier), written to `./applications/<company>-<role>/` as both
`.md` and `.docx` (editable). Facts come **only** from the master profile; the cover
letter imitates your voice. Tailoring is selection / emphasis / rewording of true
content — **never** an invented employer, title, date, degree, metric, or skill; JD
requirements you don't meet are recorded as an honesty note, not faked. Drafts stay
**local** (git-ignored, never published); the website only shows a "drafts ready" tag.
Idempotent — already-drafted roles are skipped unless `--regenerate`.

## Company discovery (propose-only)

`job-agent discover` uses Claude **web search** to propose new Bay-Area media /
entertainment / consumer companies that fit your full profile, **excludes** anything
already on your watchlist or already proposed/dismissed, and **independently verifies**
each candidate — resolving a real public ATS feed, or else checking the cited careers
URL is reachable. Only verified candidates are **proposed** (with a citable source);
the rest go to an "unverified — not proposed" bucket. **Nothing is auto-added.** Review
proposals on the website ("🧭 Companies to consider") or `discover --list`, then
`approve <id>` (appends to `companies.yaml`) or `dismiss <id>` — or run `job-agent review`
for a guided pass. Runs at most weekly (`--force` overrides). Grounding: a company is
never proposed on the model's word alone.

## Acting on results — buttons, no terminal

Run the local app and just click:

```bash
job-agent serve        # opens http://127.0.0.1:8765 with working buttons
```

Every role has **Reject** (hide it) and **Save** (flag it) buttons; every company proposal
has **Approve** / **Dismiss**. Clicks write straight to the database and update the page
instantly. (GitHub Pages is static and can't receive a click, so this small app serves the
same page from your Mac, bound to localhost only — no auth, single user.)

**Always-on (never touch the terminal again):** load the serve agent once and the app runs
in the background at a permanent bookmark:

```bash
cp scripts/com.jobagent.serve.plist ~/Library/LaunchAgents/
launchctl load   ~/Library/LaunchAgents/com.jobagent.serve.plist   # always live at :8765
launchctl unload ~/Library/LaunchAgents/com.jobagent.serve.plist   # stop it
```

**It learns:** Reject/Save are remembered (the `feedback` table) and fed into future
deep-scoring, so the agent gets better at what it surfaces. Rejected roles are hidden and
never re-added. The published GitHub Pages page stays as a read-only mirror (handy on a
phone). Terminal equivalents also exist: `job-agent review` (guided), `reject <id>`,
`save <id>`, `approve <id>`, `dismiss <id>`.

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
run emails a short nudge to `NOTIFY_EMAIL` (defaults to muhammad.e.eltahir@gmail.com):
counts ("N new — X strong, Y worth a look"), the top 3 roles, **drafts ready for**
those roles, and the **new company-proposal count**, plus the site link. It sends
nothing when there are **0 new roles AND 0 proposals**; missing creds → skipped
cleanly. Never blocks the run.

---

## Scheduling (every 2 days, launchd — macOS)

`run --if-due` runs the **full pipeline** (fetch → master-profile → score → discover
→ drafts → publish → email) and **skips if <47h since the last success** (so a
wake-from-sleep catch-up never double-runs); `--force` overrides. Discovery has its
own weekly guard. `scripts/run_scheduled.sh` wraps it; `scripts/com.jobagent.run.plist`
triggers it every 2 days. **Activate it** (not auto-loaded):

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
  schema.sql        DDL: jobs, scores, feedback, notifications, drafts, suggestions, meta
  models.py         Job dataclass (+ dedup fingerprint)
  store.py          job upsert/dedup, scoring writes, seen-state, feedback, drafts, suggestions
  companies.py      companies.yaml loader (watchlist schema)
  queries.py        Adzuna default query set (optional --adzuna source)
  textutil.py       HTML->text + epoch->ISO helpers
  tiers.py          tier thresholds (Strong / Worth a look) — shared
  drive.py          Google Drive (read-only) client for master-profile materials
  drafting.py       tailored resume + cover-letter drafts (md + docx), grounded
  discovery.py      weekly company discovery (web-search + verify, propose-only)
  website.py        self-contained site (static for Pages; interactive for the local app)
  server.py         local interactive web app (the site with working buttons)
  notify.py         optional Gmail-SMTP nudge (counts + drafts + proposals)
  sources/
    base.py         JobSource interface + JobQuery (extension point)
    ats.py          public ATS HTTP layer (endpoints, raw fetch, probe)
    resolver.py     ats=auto board resolver + unresolved reporting
    ats_sources.py  Greenhouse / Lever / Ashby / Workable / SmartRecruiters / Workday
    location.py     Bay-Area / US-remote location filter (positive US-signal)
    watchlist.py    WatchlistSource orchestration (default source)
    adzuna.py       AdzunaSource (optional, behind --adzuna)
  reasoning/
    llm.py          Anthropic Messages API seam (concurrent asyncio + caching + Batch + web search)
    profile.py      resume -> cached structured profile (+ master-profile resolver)
    master_profile.py  synthesize master + voice profile from all Drive documents
    scoring.py      triage (haiku) + deep score (sonnet/opus) vs. full profile
  digest.py         company/tier Markdown digest + seen-state
  cli.py            command-line entrypoint
scripts/
  run_scheduled.sh           launchd wrapper (run --if-due)
  com.jobagent.run.plist     launchd job: full pipeline every 2 days
  com.jobagent.serve.plist   launchd job: always-on local app (buttons) at :8765
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

### Final feature pass (current)

- [x] **A0 — Master profile from Google Drive:** read-only service-account client;
  synthesizes all resumes/CVs/cover letters into one cached master profile (+ voice
  profile), strictly grounded, with provenance + reconciled `variances`.
- [x] **A1 — Scoring re-pointed at the full profile:** triage + deep prompts weigh
  every experience thread (strategy, ops, chief of staff, BD, dealmaking, analytics,
  GM, media), not just the latest title.
- [x] **A2 — Resume + cover-letter drafting:** per-role tailored `.md` + `.docx` from
  the master profile in your voice; absolute grounding; local-only; idempotent.
- [x] **B — Weekly company discovery:** web-search proposals, verified to a real feed
  or careers page, propose-only; `approve`/`dismiss`; website "Companies to consider".
- [x] **C — Email nudge activated:** counts + top 3 + drafts-ready + proposal count;
  sends only when there's something new.
- [x] **D — Schedule:** `run --if-due` now runs the full pipeline; launchd plist (2-day).

**89 offline tests, all green.** Liberal filtering (Worth-a-look ≥ 30; triage drops
only clear hard-no roles). The master profile requires the Drive API enabled +
folder shared with the service account. Workable still fixture-only.

**Deferred (not built):** application status tracking, inbox monitoring, a web UI for
feedback/approvals.

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
