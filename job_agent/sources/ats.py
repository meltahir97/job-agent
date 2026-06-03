"""ATS HTTP layer: public job-board endpoints, raw fetch, and existence probe.

This module ONLY talks to the documented public ATS endpoints and returns the raw
job dicts; normalization into `Job` records lives in the per-ATS Source classes.
The resolver uses `probe()` to confirm a board actually exists before trusting it —
we never invent a feed URL or fabricate a board.

Endpoints:
  greenhouse : https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  lever      : https://api.lever.co/v0/postings/{slug}?mode=json
  ashby      : https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
  workable   : https://apply.workable.com/api/v3/accounts/{slug}/jobs  (POST)
               fallback https://{slug}.workable.com/spi/v3/jobs (GET)
"""
from __future__ import annotations

from typing import List, Optional

ATS_NAMES = ["greenhouse", "lever", "ashby", "workable"]

_HEADERS = {"User-Agent": "job-agent/0.1 (personal job watcher)", "Accept": "application/json"}


def board_url(ats: str, slug: str) -> str:
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "workable": f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
    }.get(ats, "")


def _get_json(session, url: str, timeout: int, *, method: str = "GET", body=None):
    resp = session.request(method, url, headers=_HEADERS, timeout=timeout, json=body)
    resp.raise_for_status()
    return resp.json()


def raw_jobs(ats: str, slug: str, session, timeout: int = 20) -> List[dict]:
    """Return the raw list of job dicts for a board. Raises on HTTP error or an
    unexpected schema (so a 404/wrong-slug never looks like an empty board)."""
    if ats == "greenhouse":
        data = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("greenhouse: unexpected response schema")
        return jobs

    if ats == "lever":
        data = _get_json(session, f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout)
        if not isinstance(data, list):
            raise ValueError("lever: unexpected response schema")
        return data

    if ats == "ashby":
        data = _get_json(
            session, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout
        )
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("ashby: unexpected response schema")
        return jobs

    if ats == "workable":
        last_err: Optional[Exception] = None
        for method, url, body in (
            ("POST", f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", {}),
            ("GET", f"https://{slug}.workable.com/spi/v3/jobs", None),
        ):
            try:
                data = _get_json(session, url, timeout, method=method, body=body)
            except Exception as e:  # try the next endpoint
                last_err = e
                continue
            jobs = (data.get("results") or data.get("jobs")) if isinstance(data, dict) else None
            if isinstance(jobs, list):
                return jobs
            last_err = ValueError("workable: unexpected response schema")
        raise last_err or ValueError("workable: no public endpoint responded")

    raise ValueError(f"unknown ats '{ats}'")


def probe(ats: str, slug: str, session, timeout: int = 10) -> Optional[int]:
    """Return the open-role count if the board exists + parses, else None."""
    try:
        return len(raw_jobs(ats, slug, session, timeout))
    except Exception:
        return None
