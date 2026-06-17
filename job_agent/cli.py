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


def _publish(conn, *, dry_run: bool, email: bool = True):
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
    if email:
        print(f"   email: {notify.send_nudge(stats, config.SITE_URL or '', tops, drafted_roles=drafted_roles, proposals=proposals)}")
    else:
        print("   email: skipped (--no-email)")
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

    from . import discovery, drafting, drive, website
    from .reasoning import master_profile as mpm, profile as profile_mod, scoring
    from .reasoning.llm import LLMError

    model = config.STRONG_MODEL if getattr(args, "opus", False) else config.DEEP_MODEL

    print("== master profile ==")
    try:
        master, _voice, files = mpm.load_or_build(conn)
        print(f"   {len(files)} Drive doc(s); threads: "
              f"{', '.join((master.get('experience_threads') or [])[:6]) or '—'}")
    except (drive.DriveError, LLMError, FileNotFoundError) as e:
        print(f"   master profile unavailable ({e}); scoring will use the resume profile.")

    print("== score ==")
    try:
        prof = profile_mod.load_for_scoring(conn)
        stats = scoring.run_scoring(conn, prof, deep_model=model, batch=getattr(args, "batch", False))
        print(f"   triaged {stats['triaged']} (kept {stats['kept']}); "
              f"deep-scored {stats['deep_scored']} with {model}.")
    except (LLMError, FileNotFoundError) as e:
        print(f"   skipped scoring: {e}")

    if not getattr(args, "no_discover", False):
        print("== discover ==")
        try:
            if discovery.should_run(conn, force=getattr(args, "force", False)):
                d = discovery.discover(conn, profile_mod.load_for_scoring(conn), model=model)
                print(f"   {len(d['proposed'])} proposed, {len(d['unverified'])} unverified.")
            else:
                print(f"   skipped (a scan ran < {config.DISCOVERY_INTERVAL_DAYS}d ago; --force to override).")
        except (LLMError, FileNotFoundError) as e:
            print(f"   skipped discovery: {e}")

    if not getattr(args, "no_drafts", False):
        print("== drafts ==")
        try:
            m2, v2 = drafting.load_profiles()
            rows = website.select_master(conn)
            if not getattr(args, "drafts_all", False):
                rows = _strong_rows(conn, rows)
            gen, skipped = drafting.run_drafts(conn, rows, m2, v2, model=model,
                                               regenerate=getattr(args, "regenerate", False))
            print(f"   {gen} generated, {skipped} existing ({len(rows)} targeted).")
        except (FileNotFoundError, LLMError) as e:
            print(f"   skipped drafts: {e}")

    print("== publish ==")
    _publish(conn, dry_run=getattr(args, "dry_run", False))

    if not getattr(args, "dry_run", False):
        db.set_meta(conn, "last_success_at", store.now_iso())
    conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local interactive web app (the site with working buttons)."""
    from . import server

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)  # ensure schema exists before first request
    conn.close()
    return server.serve(port=args.port, open_browser=not args.no_open)


def cmd_publish(args: argparse.Namespace) -> int:
    """Rebuild + publish the website (and email) from already-scored data."""
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    print("== publish ==")
    _publish(conn, dry_run=args.dry_run, email=not args.no_email)
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


def _set_feedback(job_id: int, decision: str) -> int:
    conn = db.connect()
    db.init_db(conn)
    job = store.get_job(conn, job_id)
    if not job:
        print(f"error: no job with id {job_id} (ids show when you expand a row on the site)")
        conn.close()
        return 2
    store.record_feedback(conn, job_id, decision)
    conn.close()
    if decision == "dismissed":
        print(f"Rejected: [{job_id}] {job['title']} @ {job['company']} "
              "— hidden on the next publish; this also steers future scoring.")
    else:
        print(f"Saved: [{job_id}] {job['title']} @ {job['company']} — this steers future scoring.")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    return _set_feedback(args.id, "dismissed")


def cmd_save(args: argparse.Namespace) -> int:
    return _set_feedback(args.id, "saved")


def cmd_review(args: argparse.Namespace) -> int:
    """Guided triage of company proposals + sourced jobs (no IDs to remember)."""
    import sys

    from . import discovery, website

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    if not sys.stdin.isatty():
        print("`review` is interactive — run it in a terminal. Non-interactive equivalents: "
              "`job-agent approve|dismiss <id>` (companies), `job-agent reject|save <id>` (jobs).")
        conn.close()
        return 0

    n = 0
    if not args.jobs_only:
        props = store.list_suggestions(conn, "proposed")
        print(f"\n=== {len(props)} company proposal(s) ===")
        for s in props:
            board = f"{s['ats']}:{s['slug']}" if s["ats"] else "careers page only (needs ats+slug to add)"
            print(f"\n  {s['company']}  [{board}]\n    {s['reason'] or ''}\n    {s['evidence_url'] or ''}")
            ans = input("    [a]pprove  [d]ismiss  [Enter]skip  [q]uit > ").strip().lower()
            if ans == "q":
                conn.close()
                return 0
            if ans == "a":
                if s["ats"] and s["slug"]:
                    print("    " + discovery.approve(conn, s["id"]))
                else:
                    ats = input("      ats (greenhouse|lever|ashby|workable|smartrecruiters|workday) or Enter to skip: ").strip()
                    slug = input("      slug: ").strip() if ats else ""
                    print("    " + (discovery.approve(conn, s["id"], ats=ats, slug=slug) if ats and slug else "skipped (no slug)"))
                n += 1
            elif ans == "d":
                print("    " + discovery.dismiss(conn, s["id"]))
                n += 1

    if not args.suggestions_only:
        decided = store.decided_job_ids(conn)
        rows = [r for r in website.select_master(conn) if r["id"] not in decided]
        if args.new_only:
            rows = [r for r in rows if not r["notified"]]
        print(f"\n=== {len(rows)} job(s) to review (highest fit first; Ctrl-C or q to stop) ===")
        for r in rows:
            print(f"\n  [{r['fit_score']}] {r['company']} — {r['title']}  ({r['location'] or 'n/a'})\n    {r['url'] or ''}")
            ans = input("    [s]ave  [r]eject  [Enter]skip  [q]uit > ").strip().lower()
            if ans == "q":
                break
            if ans == "s":
                store.record_feedback(conn, r["id"], "saved")
                print("    saved")
                n += 1
            elif ans == "r":
                store.record_feedback(conn, r["id"], "dismissed")
                print("    rejected")
                n += 1

    conn.close()
    print(f"\nDone — {n} decision(s) recorded. Rejected roles are hidden on the next publish and, "
          "with your saves, steer future scoring. Run `job-agent publish` to refresh the site.")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    """Draft a tailored resume + cover letter for ONE job by id — even a non-match."""
    from . import drafting
    from .reasoning.llm import LLMError

    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    job = store.get_job(conn, args.id)
    if not job:
        print(f"error: no job with id {args.id} (ids show when you expand a row on the site)")
        conn.close()
        return 2
    try:
        master, voice = drafting.load_profiles()
        res = drafting.generate_for_role(
            conn, job, master, voice,
            model=(config.STRONG_MODEL if args.opus else config.DEEP_MODEL),
            regenerate=args.regenerate,
        )
    except (FileNotFoundError, LLMError) as e:
        print(f"error: {e}")
        conn.close()
        return 2
    conn.close()
    if res is None:
        print(f"Already drafted (use --regenerate): [{args.id}] {job['title']} @ {job['company']}")
    elif res.get("where") == "drive":
        print(f"Drafted to Drive: {job['title']} @ {job['company']}\n  {res['folder']}")
    else:
        print(f"Drafted locally: {res['folder']}")
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
    dr = sub.add_parser("drafts", help="Generate tailored resume + cover-letter drafts (to Drive)")
    dr.add_argument("--all", action="store_true", help="draft for ALL tiers, not just Strong matches")
    dr.add_argument("--regenerate", action="store_true", help="overwrite existing drafts")
    dr.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for drafting")
    dr.set_defaults(func=cmd_drafts)
    df = sub.add_parser("draft", help="Draft for ONE job by id — even one not flagged as a match")
    df.add_argument("id", type=int, help="job id (shown when you expand a row on the site)")
    df.add_argument("--regenerate", action="store_true", help="overwrite an existing draft")
    df.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for drafting")
    df.set_defaults(func=cmd_draft)
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

    rv = sub.add_parser("review", help="Guided triage: approve/dismiss proposals + save/reject jobs")
    rv.add_argument("--jobs-only", action="store_true", help="only review sourced jobs")
    rv.add_argument("--suggestions-only", action="store_true", help="only review company proposals")
    rv.add_argument("--new-only", action="store_true", help="only review roles new since the last publish")
    rv.set_defaults(func=cmd_review)
    rj = sub.add_parser("reject", help="Hide a sourced role (and teach future scoring)")
    rj.add_argument("id", type=int, help="job id (shown when you expand a row on the site)")
    rj.set_defaults(func=cmd_reject)
    sv = sub.add_parser("save", help="Mark a sourced role as interesting (teaches future scoring)")
    sv.add_argument("id", type=int, help="job id (shown when you expand a row on the site)")
    sv.set_defaults(func=cmd_save)

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
    r.add_argument("--force", action="store_true", help="ignore the --if-due guard (also forces discovery)")
    r.add_argument("--no-discover", action="store_true", help="skip the weekly company-discovery step")
    r.add_argument("--no-drafts", action="store_true", help="skip generating application drafts")
    r.add_argument("--drafts-all", action="store_true", help="draft for ALL tiers, not just Strong matches")
    r.add_argument("--regenerate", action="store_true", help="overwrite existing drafts")
    r.set_defaults(func=cmd_run)

    pub = sub.add_parser("publish", help="Rebuild + publish the website (and optional email) from scored data")
    pub.add_argument("--dry-run", action="store_true", help="build the site locally + print; don't push or email")
    pub.add_argument("--no-email", action="store_true", help="push the site but don't send the email nudge")
    pub.set_defaults(func=cmd_publish)
    srv = sub.add_parser("serve", help="Run the local interactive app (the site with working buttons)")
    srv.add_argument("--port", type=int, default=8765, help="localhost port (default 8765)")
    srv.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    srv.set_defaults(func=cmd_serve)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
