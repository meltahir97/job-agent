#!/bin/bash
# job-agent scheduled run (invoked by launchd every ~2 days).
# `run --if-due` runs the FULL pipeline — fetch -> master-profile -> score ->
# discover -> drafts -> publish -> email — and self-skips if a successful run
# happened <47h ago, so a launchd wake after the Mac was asleep self-corrects
# without double-running. Discovery has its own weekly cadence guard.
set -euo pipefail
cd "/Users/muhammadeltahir/Projects/Job Search"
exec ./.venv/bin/python -m job_agent run --if-due >> data/run.log 2>&1
