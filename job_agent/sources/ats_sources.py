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

    def __init__(self, slug: str, company: str, session=None, timeout: int = 20, **extra):
        self.slug = slug
        self.company = company
        self.timeout = timeout
        self.extra = extra  # source-specific config (e.g. Workday dc/site); ignored by most
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


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


class SmartRecruitersSource(JobSource):
    """SmartRecruiters public postings API: api.smartrecruiters.com/v1/companies/{slug}/postings.

    Note: the postings *list* has no full JD, so description is composed from
    function/department/level fields (enough for triage; thinner for deep scoring).
    Unknown companies return 200 + empty (grounding-safe: no board => no rows).
    """

    name = ats = "smartrecruiters"
    max_results = 400

    def __init__(self, slug, company, session=None, timeout: int = 20, **extra):
        self.slug = slug
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        out: List[Job] = []
        offset, page = 0, 100
        headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}
        while True:
            url = f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings?limit={page}&offset={offset}"
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            out.extend(self._normalize(r) for r in content)
            offset += page
            if not content or offset >= (data.get("totalFound") or 0) or len(out) >= self.max_results:
                break
        return out[: self.max_results]

    def _normalize(self, raw: dict) -> Job:
        loc = raw.get("location") or {}
        location = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p) or None
        remote = True if loc.get("remote") else _remote_from_text(location, raw.get("name"))
        dept = (raw.get("department") or {}).get("label")
        func = (raw.get("function") or {}).get("label")
        level = (raw.get("experienceLevel") or {}).get("label")
        emp = (raw.get("typeOfEmployment") or {}).get("label")
        desc = " · ".join(x for x in (func or dept, level, emp, location) if x) or None
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id")),
            title=raw.get("name"),
            company=self.company,
            location=location,
            remote=remote,
            description=desc,
            url=f"https://jobs.smartrecruiters.com/{self.slug}/{raw.get('id')}",
            posted_at=raw.get("releasedDate"),
            category=dept,
            raw=raw,
        )


class WorkdaySource(JobSource):
    """Public Workday CXS feed: POST {tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs.

    Needs tenant (slug) + dc (e.g. wd5) + site (career-site id) from companies.yaml.
    The list payload has no JD and only a relative 'Posted X ago' date, so description
    is minimal and posted_at is left null (first_seen_at tracks recency). Some tenants
    block bots (401/403) -> raises, surfaced as UNRESOLVED by the caller.
    """

    name = ats = "workday"
    max_results = 400

    def __init__(self, slug, company, session=None, timeout: int = 20, **extra):
        self.slug = slug  # Workday tenant
        self.company = company
        self.timeout = timeout
        self.dc = extra.get("dc")
        self.site = extra.get("site")
        self.session = session or requests.Session()
        if not (self.slug and self.dc and self.site):
            raise ValueError(f"workday source for {company!r} needs slug(tenant) + dc + site")

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        base = f"https://{self.slug}.{self.dc}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{self.slug}/{self.site}/jobs"
        view = f"{base}/en-US/{self.site}"
        headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json", "Content-Type": "application/json"}
        out: List[Job] = []
        offset, page = 0, 20
        while True:
            resp = self.session.post(
                api, headers=headers,
                json={"appliedFacets": {}, "limit": page, "offset": offset, "searchText": ""},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            postings = data.get("jobPostings") or []
            out.extend(self._normalize(p, view) for p in postings)
            offset += page
            if not postings or offset >= (data.get("total") or 0) or len(out) >= self.max_results:
                break
        return out[: self.max_results]

    def _normalize(self, raw: dict, view_base: str) -> Job:
        path = raw.get("externalPath") or ""
        loc = raw.get("locationsText")
        return Job(
            source=self.ats,
            source_job_id=path or str(raw.get("title")),
            title=raw.get("title"),
            company=self.company,
            location=loc,
            remote=_remote_from_text(loc, raw.get("title")),
            description=" · ".join(x for x in (raw.get("title"), loc) if x) or None,
            url=(view_base + path) if path else None,
            posted_at=None,  # list gives only a relative date; first_seen_at tracks recency
            category=None,
            raw=raw,
        )


SOURCE_BY_ATS = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
    "smartrecruiters": SmartRecruitersSource,
    "workday": WorkdaySource,
}
