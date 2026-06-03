"""Greenhouse / Lever / Ashby / Workable sources behind the JobSource interface.

Each is constructed with a board `slug` and the watchlist `company` display name,
fetches that board via `ats.raw_jobs`, and normalizes into the SAME `Job` schema
the existing pipeline uses. Grounding rules hold:
  * unmapped fields stay None (no fabrication),
  * salary is set only when the ATS gives an employer-stated min/max,
  * remote is True only on an explicit signal, else None (never False).
"""
from __future__ import annotations

from typing import List, Optional

import requests

from ..models import Job
from ..textutil import epoch_ms_to_iso, html_to_text
from . import ats as ats_mod
from .base import JobQuery, JobSource


def _remote_from_text(*texts: Optional[str]) -> Optional[bool]:
    hay = " ".join(t for t in texts if t).lower()
    if "remote" in hay or "work from home" in hay or "anywhere" in hay:
        return True
    return None


class _AtsSource(JobSource):
    ats = "base"

    def __init__(self, slug: str, company: str, session=None, timeout: int = 20):
        self.slug = slug
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        raw = ats_mod.raw_jobs(self.ats, self.slug, self.session, self.timeout)
        jobs = [self._normalize(r) for r in raw]
        if query and query.max_results:
            jobs = jobs[: query.max_results]
        return jobs

    def _normalize(self, raw: dict) -> Job:  # pragma: no cover - overridden
        raise NotImplementedError


class GreenhouseSource(_AtsSource):
    name = ats = "greenhouse"

    def _normalize(self, raw: dict) -> Job:
        loc = (raw.get("location") or {}).get("name")
        depts = raw.get("departments") or []
        category = depts[0].get("name") if depts and isinstance(depts[0], dict) else None
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id")),
            title=raw.get("title"),
            company=self.company,
            location=loc,
            remote=_remote_from_text(raw.get("title"), loc),
            description=html_to_text(raw.get("content")),
            url=raw.get("absolute_url"),
            posted_at=raw.get("updated_at") or raw.get("first_published"),
            category=category,
            raw=raw,  # salary not exposed by the board API -> stays None
        )


class LeverSource(_AtsSource):
    name = ats = "lever"

    def _normalize(self, raw: dict) -> Job:
        cats = raw.get("categories") or {}
        loc = cats.get("location")
        workplace = (raw.get("workplaceType") or "").lower()
        remote = True if workplace == "remote" else _remote_from_text(loc, raw.get("text"))
        sr = raw.get("salaryRange") or {}
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id")),
            title=raw.get("text"),
            company=self.company,
            location=loc,
            remote=remote,
            description=raw.get("descriptionPlain") or html_to_text(raw.get("description")),
            url=raw.get("hostedUrl") or raw.get("applyUrl"),
            salary_min=sr.get("min"),
            salary_max=sr.get("max"),
            salary_currency=sr.get("currency"),
            category=cats.get("team") or cats.get("commitment"),
            posted_at=epoch_ms_to_iso(raw.get("createdAt")),
            raw=raw,
        )


class AshbySource(_AtsSource):
    name = ats = "ashby"

    def _normalize(self, raw: dict) -> Job:
        loc = raw.get("location")
        remote = True if raw.get("isRemote") else _remote_from_text(loc, raw.get("title"))
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id")),
            title=raw.get("title"),
            company=self.company,
            location=loc,
            remote=remote,
            description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
            url=raw.get("jobUrl") or raw.get("applyUrl"),
            posted_at=raw.get("publishedAt") or raw.get("publishedDate"),
            category=raw.get("department") or raw.get("team"),
            raw=raw,  # Ashby comp is a nested tier structure, not a simple min/max -> salary None
        )


class WorkableSource(_AtsSource):
    name = ats = "workable"

    def _normalize(self, raw: dict) -> Job:
        loc = raw.get("location")
        if isinstance(loc, dict):
            parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            location = ", ".join(p for p in parts if p) or loc.get("location_str")
            telework = bool(loc.get("telecommuting")) or str(loc.get("workplace", "")).lower() == "remote"
        else:
            location = loc if isinstance(loc, str) else None
            telework = False
        remote = True if telework else _remote_from_text(location, raw.get("title"))
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id") or raw.get("shortcode")),
            title=raw.get("title"),
            company=self.company,
            location=location,
            remote=remote,
            description=html_to_text(raw.get("description")),
            url=raw.get("application_url") or raw.get("url") or raw.get("shortlink"),
            posted_at=raw.get("published_on") or raw.get("created_at") or raw.get("published"),
            category=raw.get("department") or raw.get("function"),
            raw=raw,
        )


SOURCE_BY_ATS = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
}
