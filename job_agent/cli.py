"""Command-line entrypoint.

Top-level imports stay dependency-free so `db init` works on a bare interpreter;
anything needing third-party packages (requests/Adzuna) is imported lazily inside
the command that uses it.
"""
from __future__ import annotations

import argparse
import sys

from . import config, db, queries, store
from .sources.base import JobQuery


def cmd_db_init(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    conn.close()
    print(f"Initialized database at {config.DB_PATH}")
    print(f"schema_version={db.SCHEMA_VERSION}  tables: {', '.join(tables)}")
    return 0


def _run_fetch(conn, qs) -> tuple:
    """Fetch each query via Adzuna and upsert. Returns (processed, new)."""
    import requests  # lazy: keeps `db init` dependency-free

    from .sources.adzuna import AdzunaConfigError, AdzunaSource

    try:
        src = AdzunaSource(config.ADZUNA_APP_ID, config.ADZUNA_APP_KEY, country=config.COUNTRY)
    except AdzunaConfigError as e:
        print(f"error: {e}")
        print("Add ADZUNA_APP_ID and ADZUNA_APP_KEY to .env (see .env.example).")
        return None  # signal config failure

    processed = new = 0
    for q in qs:
        label = q.location or ("remote" if q.remote else "any")
        try:
            jobs = src.fetch(q)
        except requests.HTTPError as e:
            print(f"  ! HTTP error for {q.keywords!r} @ {label}: {e}")
            continue
        except requests.RequestException as e:
            print(f"  ! network error for {q.keywords!r} @ {label}: {e}")
            continue
        n_new = 0
        for job in jobs:
            _, is_new = store.upsert_job(conn, job)
            n_new += int(is_new)
        processed += len(jobs)
        new += n_new
        print(f"  {q.keywords!r} @ {label}: {len(jobs)} fetched, {n_new} new")
    return processed, new


def _run_watchlist(conn):
    """Poll the company watchlist via ATS feeds, location-filter, upsert.

    Returns (in_scope, new) or None if the watchlist can't be loaded.
    """
    from .companies import CompaniesError, load_companies
    from .sources.watchlist import WatchlistSource

    try:
        companies = load_companies()
    except (FileNotFoundError, CompaniesError) as e:
        print(f"error: {e}")
        print(f"Edit your watchlist at {config.COMPANIES_PATH}.")
        return None

    print(f"  watchlist: {len(companies)} companies from {config.COMPANIES_PATH.name}")
    jobs, report = WatchlistSource(companies).collect()

    new = 0
    for job in jobs:
        _, is_new = store.upsert_job(conn, job)
        new += int(is_new)

    for r in report.fetched_ok:
        print(f"    + {r.company} [{r.resolution.ats}]: {r.fetched} fetched, {r.kept} in-scope")
    for r in report.errored:
        print(f"    ! {r.company}: {r.error}")
    if report.unresolved:
        print("  UNRESOLVED — add `ats` + `slug` in companies.yaml for:")
        for r in report.unresolved:
            print(f"      - {r.company}")
    return len(jobs), new


def cmd_fetch(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)

    if args.adzuna:
        print("== adzuna (keyword source) ==")
        if args.what:
            qs = [JobQuery(keywords=args.what, location=args.where, max_results=args.max, max_days_old=args.days)]
        else:
            qs = queries.default_queries(max_results=args.max, max_days_old=args.days)
        result = _run_fetch(conn, qs)
    else:
        print("== watchlist ==")
        result = _run_watchlist(conn)

    if result is None:
        conn.close()
        return 2
    processed, new = result
    print(f"Done: {processed} in-scope, {new} new, {store.count_jobs(conn)} total in DB.")
    conn.close()
    return 0


def _git_publish(site_path) -> bool:
    """Commit ./docs and push. Returns True only if pushed to a remote."""
    import subprocess

    def git(*a):
        return subprocess.run(["git", *a], cwd=str(config.BASE_DIR), capture_output=True, text=True)

    git("add", "docs")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("   no site changes to commit.")
    else:
        git("commit", "-m", "Publish job site")
    if not git("remote").stdout.strip():
        print("   ! no git remote configured — site built at docs/ but NOT pushed.")
        print("     See README → Publishing to enable GitHub Pages, then re-run `publish`.")
        return False
    r = git("push")
    if r.returncode != 0:
        print(f"   ! git push failed: {r.stderr.strip()[:200]}")
        return False
    print("   pushed docs/ to remote (GitHub Pages will update shortly).")
    return True


def _publish(conn, *, dry_run: bool):
    """Build the site; on a real run also commit/push + email + clear NEW state."""
    from datetime import datetime

    from . import notify, website

    path, stats, rows = website.build_site(conn, generated_at=datetime.now().astimezone())
    tops = notify.top_roles(rows)
    drafted_roles = [f"{r['company']} – {r['title']}" for r in rows if r["drafted"]][:8]
    proposals = len(store.list_suggestions(conn, "proposed"))
    print(f"   site -> {path}  ({stats['strong']} strong, {stats['look']} worth a look, "
          f"{stats['new']} new, {stats['companies']} companies; {len(drafted_roles)} drafted, {proposals} proposals)")
    subject, body = notify.render_nudge(stats, config.SITE_URL or "", tops,
                                        drafted_roles=drafted_roles, proposals=proposals)
    if dry_run:
        print("   [dry-run] would NOT push or email. Email that WOULD send:")
        print(f"     Subject: {subject}")
        for line in body.splitlines():
            print(f"     | {line}")
        return stats
    pushed = _git_publish(path)
    email_status = notify.send_nudge(stats, config.SITE_URL or "", tops,
                                     drafted_roles=drafted_roles, proposals=proposals)
    print(f"   email: {email_status}")
    if pushed:
        website.mark_published(conn, rows)  # clear NEW only after a successful publish
    return stats


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline: fetch -> score -> publish (website + optional email)."""
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)

    # Schedule guard: skip if a successful run happened recently (unless --force).
    if getattr(args, "if_due", False) and not getattr(args, "force", False):
        last = db.get_meta(conn, "last_success_at")
        if last:
            from datetime import datetime, timezone
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h < 47:
                    print(f"Skipping: last success {age_h:.1f}h ago (<47h). Use --force to override.")
                    conn.close()
                    return 0
            except ValueError:
                pass

    print("== fetch ==")
    if getattr(args, "adzuna", False):
        result = _run_fetch(conn, queries.default_queries(max_results=args.max, max_days_old=args.days))
    else:
        result = _run_watchlist(conn)
    if result is None:
        conn.close()
        return 2
    processed, new = result
    print(f"   {processed} in-scope, {new} new, {store.count_jobs(conn)} total in DB.")

    print("== score ==")
    from .reasoning import profile as profile_mod, scoring
    from .reasoning.llm import LLMError

    model = config.STRONG_MODEL if getattr(args, "opus", False) else config.DEEP_MODEL
    try:
        prof = profile_mod.load_for_scoring(conn)
        stats = scoring.run_scoring(conn, prof, deep_model=model, batch=getattr(args, "batch", False))
        print(f"   triaged {stats['triaged']} (kept {stats['kept']}); "
              f"deep-scored {stats['deep_scored']} with {model}.")
    except (LLMError, FileNotFoundError) as e:
        print(f"   skipped scoring: {e}")

    print("== publish ==")
    _publish(conn, dry_run=getattr(args, "dry_run", False))

    if not getattr(args, "dry_run", False):
        db.set_meta(conn, "last_success_at", store.now_iso())
    conn.close()
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Rebuild + publish the website (and email) from already-scored data."""
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    print("== publish ==")
    _publish(conn, dry_run=args.dry_run)
    conn.close()
    return 0


def cmd_master_profile(args: argparse.Namespace) -> int:
    """Build (or refresh) the master profile from ALL Drive materials, then summarize."""
    from . import drive
    from .reasoning import master_profile as mpm
    from .reasoning.llm import LLMError

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    try:
        master, voice, files = mpm.load_or_build(conn, force=args.force)
    except drive.DriveError as e:
        print(f"error: {e}")
        conn.close()
        return 2
    except (LLMError, FileNotFoundError) as e:
        print(f"error: {e}")
        conn.close()
        return 2
    conn.close()

    print(f"Master profile -> {config.MASTER_PROFILE_PATH}")
    print(f"  built from {len(files)} Drive document(s):")
    for f in files:
        tag = " [cover letter]" if drive.is_cover_letter(f) else ""
        print(f"    - {f.get('name')}  ({str(f.get('modifiedTime'))[:10]}){tag}")
    print(f"  name: {master.get('name')}  | seniority: {master.get('seniority')}  | yrs: {master.get('years_experience')}")
    print(f"  threads: {', '.join(master.get('experience_threads') or []) or '—'}")
    print(f"  employers: {len(master.get('employers') or [])}  | achievements: {len(master.get('achievements') or [])}")
    covers = (voice.get("_meta", {}) or {}).get("cover_letters_used") or []
    print(f"  voice: {'approximate' if voice.get('approximate') else 'captured'} from {len(covers)} cover letter(s)")
    if master.get("variances"):
        print(f"  variances reconciled: {len(master['variances'])}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .reasoning import profile as profile_mod
    from .reasoning.llm import LLMError

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    try:
        prof = profile_mod.load_or_build(conn, force=args.force)
    except FileNotFoundError as e:
        print(f"error: {e}")
        conn.close()
        return 2
    except LLMError as e:
        print(f"error: {e}")
        conn.close()
        return 2
    conn.close()

    meta = prof.get("_meta", {})
    print(f"Profile ready -> {config.PROFILE_PATH}")
    print(f"  name:      {prof.get('name')}")
    print(f"  seniority: {prof.get('seniority')}  | years: {prof.get('years_experience')}")
    print(f"  domains:   {', '.join(prof.get('domains') or []) or '—'}")
    print(f"  skills:    {len(prof.get('skills') or [])} listed")
    print(f"  targets:   {', '.join(prof.get('target_titles') or []) or '—'}")
    if meta:
        print(f"  built with {meta.get('model')} at {meta.get('built_at')}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from .reasoning import profile as profile_mod, scoring
    from .reasoning.llm import LLMError

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    model = config.STRONG_MODEL if args.opus else config.DEEP_MODEL
    try:
        prof = profile_mod.load_for_scoring(conn)
        stats = scoring.run_scoring(conn, prof, deep_model=model, batch=getattr(args, "batch", False))
    except (FileNotFoundError, LLMError) as e:
        print(f"error: {e}")
        conn.close()
        return 2
    conn.close()
    print(
        f"Triaged {stats['triaged']} (kept {stats['kept']}); "
        f"deep-scored {stats['deep_scored']} with {model}."
    )
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from . import digest as digest_mod

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    path, count, _ = digest_mod.write_digest(
        conn, min_score=args.min_score, limit=args.limit, only_unnotified=not args.all
    )
    conn.close()
    if count == 0:
        print("No new qualifying roles to write (use --all to re-include sent roles, or run `score`).")
        return 0
    print(f"Wrote {count} role(s) -> {path}")
    return 0


def _strong_rows(conn, rows):
    from .tiers import tier_for
    return [r for r in rows if tier_for(r["fit_score"], r["label"]) == "strong"]


def cmd_drafts(args: argparse.Namespace) -> int:
    """Generate tailored resume + cover-letter drafts for surfaced roles (local only)."""
    from . import drafting, website

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    try:
        master, voice = drafting.load_profiles()
    except FileNotFoundError as e:
        print(f"error: {e}")
        conn.close()
        return 2
    rows = website.select_master(conn)
    if not args.all:
        rows = _strong_rows(conn, rows)
    model = config.STRONG_MODEL if args.opus else config.DEEP_MODEL
    gen, skipped = drafting.run_drafts(conn, rows, master, voice, model=model, regenerate=args.regenerate)
    conn.close()
    scope = "all tiers" if args.all else "Strong matches"
    print(f"Drafts ({scope}): {gen} generated, {skipped} already existed; {len(rows)} role(s) targeted.")
    print(f"  -> {config.APPLICATIONS_DIR}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Propose new target companies (web-search + verify). Propose-only; never auto-added."""
    from . import discovery
    from .reasoning import profile as profile_mod
    from .reasoning.llm import LLMError

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)

    if args.list:
        props = store.list_suggestions(conn, "proposed")
        if not props:
            print("No open company proposals. Run `job-agent discover`.")
        for s in props:
            board = f"{s['ats']}:{s['slug']}" if s["ats"] else "careers page"
            print(f"  [{s['id']}] {s['company']}  ({board})  {s['evidence_url'] or ''}")
            print(f"        {s['reason'] or ''}")
        conn.close()
        return 0

    if not discovery.should_run(conn, force=args.force):
        last = db.get_meta(conn, "last_discovery_at")
        print(f"Skipping discovery: last scan {last} (< {config.DISCOVERY_INTERVAL_DAYS}d ago). Use --force.")
        conn.close()
        return 0
    try:
        prof = profile_mod.load_for_scoring(conn)
        model = config.STRONG_MODEL if args.opus else config.DEEP_MODEL
        res = discovery.discover(conn, prof, model=model)
    except (FileNotFoundError, LLMError) as e:
        print(f"error: {e}")
        conn.close()
        return 2

    print(f"Discovery: {len(res['proposed'])} proposed, {len(res['unverified'])} unverified.")
    for s in store.list_suggestions(conn, "proposed"):
        board = f"{s['ats']}:{s['slug']}" if s["ats"] else "careers page"
        print(f"  [{s['id']}] + {s['company']}  ({board})  {s['evidence_url'] or ''}")
        print(f"        {s['reason'] or ''}")
    for u in res["unverified"]:
        print(f"  ? {u['company']} — unverified, not proposed {u.get('evidence_url') or ''}")
    if res["proposed"]:
        print("Approve: job-agent approve <id>   |   Dismiss: job-agent dismiss <id>")
    conn.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from . import discovery
    conn = db.connect()
    db.init_db(conn)
    print(discovery.approve(conn, args.id, ats=args.ats, slug=args.slug))
    conn.close()
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    from . import discovery
    conn = db.connect()
    db.init_db(conn)
    print(discovery.dismiss(conn, args.id))
    conn.close()
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    if args.list:
        rows = store.list_feedback(conn)
        if not rows:
            print("No feedback recorded yet.")
        for r in rows:
            note = f"  ({r['note']})" if r["note"] else ""
            print(f"  [{r['job_id']}] {r['decision']:9} {r['title']} @ {r['company']}{note}")
        conn.close()
        return 0
    if args.job_id is None or not (args.saved or args.dismissed):
        print("usage: job-agent feedback <job_id> --saved|--dismissed [--note ...]   (or --list)")
        conn.close()
        return 2
    job = store.get_job(conn, args.job_id)
    if not job:
        print(f"error: no job with id {args.job_id} (ids are shown in the digest)")
        conn.close()
        return 2
    decision = "saved" if args.saved else "dismissed"
    store.record_feedback(conn, args.job_id, decision, args.note)
    conn.close()
    print(f"Recorded {decision}: [{args.job_id}] {job['title']} @ {job['company']}")
    print("This now informs future deep-scoring runs.")
    return 0


def _add_fetch_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--adzuna", action="store_true",
        help="use the Adzuna keyword source instead of the company watchlist (default)",
    )
    p.add_argument("--what", help="[adzuna] ad-hoc keyword search (overrides the default query set)")
    p.add_argument("--where", help="[adzuna] location for the ad-hoc search, e.g. 'San Francisco'")
    p.add_argument("--max", type=int, default=50, help="[adzuna] max results per query (default 50)")
    p.add_argument("--days", type=int, default=30, help="[adzuna] max age in days (default 30)")


def _add_digest_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--min-score", type=int, default=config.TIER_LOOK_MIN,
                   help=f"minimum fit score to include (default {config.TIER_LOOK_MIN} = 'worth a look')")
    p.add_argument("--limit", type=int, default=None, help="max roles to include")
    p.add_argument("--all", action="store_true", help="include roles already sent in a previous digest")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job-agent",
        description="Personal job-discovery agent (fetch -> score -> digest).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    db_p = sub.add_parser("db", help="Database admin")
    db_sub = db_p.add_subparsers(dest="subcommand", required=True)
    db_sub.add_parser("init", help="Create the SQLite database + schema").set_defaults(
        func=cmd_db_init
    )

    f = sub.add_parser("fetch", help="Fetch + store listings (company watchlist by default; --adzuna for keyword search)")
    _add_fetch_flags(f)
    f.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("profile", help="Parse resume -> cached profile JSON")
    pr.add_argument("--force", action="store_true", help="re-parse even if the resume is unchanged")
    pr.set_defaults(func=cmd_profile)
    mp_p = sub.add_parser("master-profile", help="Build the master profile from ALL Drive materials")
    mp_p.add_argument("--force", action="store_true", help="rebuild even if the Drive document set is unchanged")
    mp_p.set_defaults(func=cmd_master_profile)
    sc = sub.add_parser("score", help="Triage (haiku) + deep-score (sonnet) new jobs")
    sc.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for deep scoring")
    sc.add_argument("--batch", action="store_true", help="use the Batch API (cheaper, latency-tolerant)")
    sc.set_defaults(func=cmd_score)
    dg = sub.add_parser("digest", help="Write a ranked Markdown digest to ./digests")
    _add_digest_flags(dg)
    dg.set_defaults(func=cmd_digest)
    dr = sub.add_parser("drafts", help="Generate tailored resume + cover-letter drafts (local only)")
    dr.add_argument("--all", action="store_true", help="draft for ALL tiers, not just Strong matches")
    dr.add_argument("--regenerate", action="store_true", help="overwrite existing drafts")
    dr.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for drafting")
    dr.set_defaults(func=cmd_drafts)
    disc = sub.add_parser("discover", help="Propose NEW target companies (web-search + verify; propose-only)")
    disc.add_argument("--force", action="store_true", help="run even if a scan happened in the last week")
    disc.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for discovery")
    disc.add_argument("--list", action="store_true", help="list open proposals (no new scan)")
    disc.set_defaults(func=cmd_discover)
    ap = sub.add_parser("approve", help="Approve a company proposal -> append to companies.yaml")
    ap.add_argument("id", type=int, help="suggestion id (from `discover --list`)")
    ap.add_argument("--ats", help="ATS if not auto-resolved (greenhouse|lever|ashby|workable|smartrecruiters|workday)")
    ap.add_argument("--slug", help="board slug if not auto-resolved")
    ap.set_defaults(func=cmd_approve)
    dis = sub.add_parser("dismiss", help="Dismiss a company proposal (suppress re-proposal)")
    dis.add_argument("id", type=int, help="suggestion id (from `discover --list`)")
    dis.set_defaults(func=cmd_dismiss)

    fb = sub.add_parser("feedback", help="Mark a job saved/dismissed (tunes future scoring)")
    fb.add_argument("job_id", nargs="?", type=int, help="job id (shown in the digest)")
    grp = fb.add_mutually_exclusive_group()
    grp.add_argument("--saved", action="store_true", help="you're interested")
    grp.add_argument("--dismissed", action="store_true", help="not interested")
    fb.add_argument("--note", help="optional note")
    fb.add_argument("--list", action="store_true", help="list recorded feedback")
    fb.set_defaults(func=cmd_feedback)

    r = sub.add_parser("run", help="Full pipeline: fetch -> score -> publish (website + optional email)")
    _add_fetch_flags(r)
    r.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for deep scoring")
    r.add_argument("--batch", action="store_true", help="use the Batch API (cheaper, latency-tolerant)")
    r.add_argument("--dry-run", action="store_true", help="build the site locally + print; don't push or email")
    r.add_argument("--if-due", action="store_true", help="skip if a successful run happened <47h ago (for cron)")
    r.add_argument("--force", action="store_true", help="ignore the --if-due guard")
    r.set_defaults(func=cmd_run)

    pub = sub.add_parser("publish", help="Rebuild + publish the website (and optional email) from scored data")
    pub.add_argument("--dry-run", action="store_true", help="build the site locally + print; don't push or email")
    pub.set_defaults(func=cmd_publish)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
