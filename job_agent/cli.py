"""Command-line entrypoint.

Milestone 1 wires up the command surface and a working `db init`. The remaining
subcommands are intentional stubs filled in by later milestones, so the end-to-end
shape is visible from the start.
"""
from __future__ import annotations

import argparse
import sys

from . import config, db


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


def _todo(milestone: int):
    def run(args: argparse.Namespace) -> int:
        print(f"[not implemented yet — milestone {milestone}]")
        return 1

    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job-agent",
        description="Personal job-discovery agent (fetch -> score -> digest).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    db_p = sub.add_parser("db", help="Database admin")
    db_sub = db_p.add_subparsers(dest="subcommand", required=True)
    db_init = db_sub.add_parser("init", help="Create the SQLite database + schema")
    db_init.set_defaults(func=cmd_db_init)

    sub.add_parser("fetch", help="Fetch + store raw listings (milestone 2)").set_defaults(
        func=_todo(2)
    )
    sub.add_parser("profile", help="Parse resume -> profile JSON (milestone 3)").set_defaults(
        func=_todo(3)
    )
    sub.add_parser("score", help="Triage + deep-score jobs (milestone 4)").set_defaults(
        func=_todo(4)
    )
    sub.add_parser("digest", help="Write ranked Markdown digest (milestone 5)").set_defaults(
        func=_todo(5)
    )
    sub.add_parser("feedback", help="Mark a job saved/dismissed (milestone 7)").set_defaults(
        func=_todo(7)
    )
    sub.add_parser("run", help="Run the full pipeline end-to-end").set_defaults(func=_todo(2))

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
