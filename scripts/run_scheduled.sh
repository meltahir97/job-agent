#!/bin/bash
# job-agent scheduled run (invoked by launchd every ~2 days).
# `run --if-due` self-skips if a successful run happened <47h ago, so a launchd
# wake after the Mac was asleep self-corrects without double-running.
set -euo pipefail
cd "/Users/muhammadeltahir/Projects/Job Search"
exec ./.venv/bin/python -m job_agent run --if-due >> data/run.log 2>&1
