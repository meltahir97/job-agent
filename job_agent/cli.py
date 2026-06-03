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


def cmd_fetch(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)

    if args.what:
        qs = [
            JobQuery(
                keywords=args.what,
                location=args.where,
                max_results=args.max,
                max_days_old=args.days,
            )
        ]
    else:
        qs = queries.default_queries(max_results=args.max, max_days_old=args.days)

    result = _run_fetch(conn, qs)
    if result is None:
        conn.close()
        return 2
    processed, new = result
    print(f"Done: {processed} listings processed, {new} new, {store.count_jobs(conn)} total in DB.")
    conn.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline. Currently: fetch (later milestones add score -> digest -> feedback)."""
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    print("== fetch ==")
    result = _run_fetch(conn, queries.default_queries(max_results=args.max, max_days_old=args.days))
    if result is None:
        conn.close()
        return 2
    processed, new = result
    print(f"   {processed} processed, {new} new, {store.count_jobs(conn)} total in DB.")

    print("== score ==")
    from .reasoning import profile as profile_mod, scoring
    from .reasoning.llm import LLMError

    model = config.STRONG_MODEL if getattr(args, "opus", False) else config.DEEP_MODEL
    try:
        prof = profile_mod.load_or_build(conn)
        stats = scoring.run_scoring(conn, prof, deep_model=model)
        print(
            f"   triaged {stats['triaged']} (kept {stats['kept']}); "
            f"deep-scored {stats['deep_scored']} with {model}."
        )
    except (LLMError, FileNotFoundError) as e:
        print(f"   skipped scoring: {e}")

    print("== digest ==  [pending — milestone 5]")
    conn.close()
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
        prof = profile_mod.load_or_build(conn)
        stats = scoring.run_scoring(conn, prof, deep_model=model)
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


def _todo(milestone: int):
    def run(args: argparse.Namespace) -> int:
        print(f"[not implemented yet — milestone {milestone}]")
        return 1

    return run


def _add_fetch_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--what", help="ad-hoc keyword search (overrides default query set)")
    p.add_argument("--where", help="location for the ad-hoc search, e.g. 'San Francisco'")
    p.add_argument("--max", type=int, default=50, help="max results per query (default 50)")
    p.add_argument("--days", type=int, default=30, help="max age in days (default 30)")


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

    f = sub.add_parser("fetch", help="Fetch + store raw listings from Adzuna")
    _add_fetch_flags(f)
    f.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("profile", help="Parse resume -> cached profile JSON")
    pr.add_argument("--force", action="store_true", help="re-parse even if the resume is unchanged")
    pr.set_defaults(func=cmd_profile)
    sc = sub.add_parser("score", help="Triage (haiku) + deep-score (sonnet) new jobs")
    sc.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for deep scoring")
    sc.set_defaults(func=cmd_score)
    sub.add_parser("digest", help="Write ranked Markdown digest (milestone 5)").set_defaults(
        func=_todo(5)
    )
    sub.add_parser("feedback", help="Mark a job saved/dismissed (milestone 7)").set_defaults(
        func=_todo(7)
    )

    r = sub.add_parser("run", help="Run the full pipeline end-to-end")
    _add_fetch_flags(r)
    r.add_argument("--opus", action="store_true", help="use claude-opus-4-8 for deep scoring")
    r.set_defaults(func=cmd_run)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
