"""Greenhouse / Lever / Ashby / Workable sources behind the JobSource interface.

Each is constructed with a board `slug` and the watchlist `company` display name,
fetches that board via `ats.raw_jobs`, and normalizes into the SAME `Job` schema
the existing pipeline uses. Grounding rules hold:
  * unmapped fields stay None (no fabrication),
  * salary is set only when the ATS gives an employer-stated min/max,
  * remote is True only on an explicit signal, else None (never False).
"""
from __future__ import annotations

import html
import re
from typing import List, Optional
from urllib.parse import quote

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


def _ashby_salary(comp: Optional[dict]):
    """Extract (min, max, currency) from an Ashby compensation object.

    Reads only the structured Salary component (ignores equity / bonus / cash);
    leaves everything None when no employer-stated salary is present. Grounded:
    values come straight from the feed, never inferred.
    """
    if not isinstance(comp, dict):
        return (None, None, None)
    comps = comp.get("summaryComponents")
    if not isinstance(comps, list):
        comps = []
    for c in comps:
        if isinstance(c, dict) and c.get("compensationType") == "Salary":
            return (c.get("minValue"), c.get("maxValue"), c.get("currencyCode"))
    return (None, None, None)


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

    # Some orgs (e.g. Whatnot) disable the public posting API but still host their
    # board on jobs.ashbyhq.com — that board's public GraphQL endpoint lists the
    # postings (slim records: no JD text, so triage scores the title).
    _GQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
    _GQL = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { "
            "jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) "
            "{ jobPostings { id title locationName employmentType } } }")

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        try:
            jobs = super().fetch(query)
            if jobs:
                return jobs
        except Exception:  # posting API 404s when the org disables it
            jobs = []
        try:
            resp = self.session.post(self._GQL_URL, json={
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": self.slug},
                "query": self._GQL,
            }, headers={"User-Agent": _BROWSER_UA, "Content-Type": "application/json"},
                timeout=self.timeout)
            resp.raise_for_status()
            board = (resp.json().get("data") or {}).get("jobBoard") or {}
        except Exception:
            return jobs
        out: List[Job] = []
        for p in board.get("jobPostings") or []:
            pid = str(p.get("id") or "")
            title = (p.get("title") or "").strip()
            if not (pid and title):
                continue
            loc = p.get("locationName")
            out.append(Job(
                source=self.ats, source_job_id=pid, title=title, company=self.company,
                location=loc, remote=_remote_from_text(loc, title), description=None,
                url=f"https://jobs.ashbyhq.com/{self.slug}/{pid}",
                contract_type=p.get("employmentType"), raw=p,
            ))
        return out

    def _normalize(self, raw: dict) -> Job:
        loc = raw.get("location")
        remote = True if raw.get("isRemote") else _remote_from_text(loc, raw.get("title"))
        smin, smax, scur = _ashby_salary(raw.get("compensation"))
        return Job(
            source=self.ats,
            source_job_id=str(raw.get("id")),
            title=raw.get("title"),
            company=self.company,
            location=loc,
            remote=remote,
            description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
            url=raw.get("jobUrl") or raw.get("applyUrl"),
            salary_min=smin,
            salary_max=smax,
            salary_currency=scur,
            posted_at=raw.get("publishedAt") or raw.get("publishedDate"),
            category=raw.get("department") or raw.get("team"),
            raw=raw,  # salary parsed from compensation.summaryComponents (Salary component only)
        )


class WorkableSource(_AtsSource):
    name = ats = "workable"

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        try:
            return super().fetch(query)
        except Exception:
            pass
        # Some accounts (e.g. Linktree) close the SPI API (401) but keep the public
        # widget API open — same job records, different door.
        resp = self.session.get(
            f"https://apply.workable.com/api/v1/widget/accounts/{self.slug}?details=true",
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"}, timeout=self.timeout)
        resp.raise_for_status()
        jobs = [self._normalize(r) for r in (resp.json().get("jobs") or [])]
        if query and query.max_results:
            jobs = jobs[: query.max_results]
        return jobs

    def _normalize(self, raw: dict) -> Job:
        loc = raw.get("location")
        if isinstance(loc, dict):
            parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            location = ", ".join(p for p in parts if p) or loc.get("location_str")
            telework = bool(loc.get("telecommuting")) or str(loc.get("workplace", "")).lower() == "remote"
        else:
            location = loc if isinstance(loc, str) else None
            telework = False
        if not location:  # widget-API records carry city/state/country at the top level
            location = ", ".join(p for p in (raw.get("city"), raw.get("state"), raw.get("country")) if p) or None
        telework = telework or bool(raw.get("telecommuting"))
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


class GoogleSource(JobSource):
    """Google Careers has no public ATS feed, so we read the job links embedded in the
    public results page, scoped to YouTube / media via search queries. This is a SCRAPE
    (HTML, not an API) — inherently fragile; it returns [] gracefully if the markup
    changes, never fabricating. Per-job location/description are thin (title-derived);
    deep scoring judges relevance against the candidate profile.
    """
    name = ats = "google"
    BASE = "https://www.google.com/about/careers/applications/jobs/results/"
    _JOB = re.compile(
        r'href="jobs/results/(\d{6,})-([a-z0-9-]+)\?[^"]*"\s+aria-label="Learn more about ([^"]+)"'
    )
    DEFAULT_QUERIES = ["YouTube", "media partnerships", "content partnerships"]
    # obvious non-fits for a strategy / ops / BD / corp-dev candidate — dropped at source
    _DROP = re.compile(
        r"\b(software engineer|engineer,|hardware|silicon|firmware|ux |ux/|ui designer|"
        r"research scientist|developer relations|data center|network engineer|"
        r"security engineer|electrical|mechanical engineer|quantum|chip )\b", re.I)
    MAX_PAGES = 6  # natural bound: paginate each query until results run out (or this)

    def __init__(self, slug=None, company="Google", session=None, timeout: int = 25, **extra):
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()
        q = extra.get("queries")
        self.queries = q if isinstance(q, list) and q else self.DEFAULT_QUERIES
        self.location = extra.get("location") or "United States"

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        # No per-query cap: every query contributes all its (deduped, non-eng) roles;
        # pagination is the only bound. Deep scoring filters relevance downstream.
        out = {}
        headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html"}
        for q in self.queries:
            for page in range(1, self.MAX_PAGES + 1):
                url = f"{self.BASE}?q={quote(q)}&location={quote(self.location)}&page={page}"
                try:
                    resp = self.session.get(url, headers=headers, timeout=self.timeout)
                    resp.raise_for_status()
                except requests.RequestException:
                    break
                found = self._JOB.findall(resp.text)
                if not found:
                    break
                for jid, slug, title in found:
                    title = html.unescape(title).strip()
                    if jid in out or self._DROP.search(title):
                        continue
                    out[jid] = self._normalize(jid, slug, title)
                if len(found) < 10:  # last page for this query
                    break
        return list(out.values())

    def _normalize(self, jid: str, slug: str, title: str) -> Job:
        return Job(
            source=self.ats, source_job_id=jid, title=title, company=self.company,
            location=self.location, remote=_remote_from_text(title),
            description=f"{title}. Google Careers role ({self.location}); team/function per the title.",
            url=f"{self.BASE}{jid}-{slug}", category=None, posted_at=None,
            raw={"id": jid, "slug": slug},
        )


class NetflixSource(JobSource):
    """Netflix careers via its public Eightfold API (explore.jobs.netflix.net). Netflix
    is entirely media/entertainment, so queries are the CORE functions; the location
    filter + deep scoring keep only Bay/US-remote, on-target roles. Rich descriptions."""
    name = ats = "netflix"
    API = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
    DEFAULT_QUERIES = ["strategy", "operations", "business development", "partnerships",
                       "corporate development", "content strategy"]
    _DROP = re.compile(
        r"\b(software|engineer|engineering|developer|designer|\bux\b|\bui\b|data scientist|"
        r"machine learning|infrastructure|security engineer|technical artist|animator|recruit)\b", re.I)
    PER_QUERY = 100

    def __init__(self, slug=None, company="Netflix", session=None, timeout: int = 25, **extra):
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()
        q = extra.get("queries")
        self.queries = q if isinstance(q, list) and q else self.DEFAULT_QUERIES
        self.location = extra.get("location") or "United States"

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        out = {}
        headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}
        for q in self.queries:
            start = 0
            while start < self.PER_QUERY:
                params = {"domain": "netflix.com", "query": q, "start": start, "num": 20,
                          "location": self.location, "sort_by": "relevance"}
                try:
                    resp = self.session.get(self.API, params=params, headers=headers, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                except (requests.RequestException, ValueError):
                    break
                positions = data.get("positions") or []
                if not positions:
                    break
                for p in positions:
                    jid = str(p.get("id"))
                    title = (p.get("name") or "").strip()
                    if jid in out or not title or self._DROP.search(title):
                        continue
                    out[jid] = self._normalize(p, title)
                start += len(positions)
                if start >= (data.get("count") or 0):
                    break
        return list(out.values())

    def _normalize(self, p: dict, title: str) -> Job:
        loc = p.get("location") or (p.get("locations") or [None])[0]
        wlo = (p.get("work_location_option") or "").lower()
        remote = True if "remote" in wlo else _remote_from_text(loc, title)
        return Job(
            source=self.ats, source_job_id=str(p.get("id")), title=title, company=self.company,
            location=loc, remote=remote,
            description=(html_to_text(p.get("job_description") or "") or "")[:4000] or title,
            url=p.get("canonicalPositionUrl") or f"https://explore.jobs.netflix.net/careers/job/{p.get('id')}",
            category=p.get("department"), posted_at=None,
            raw={"id": p.get("id"), "display_job_id": p.get("display_job_id")},
        )


class AppleSource(JobSource):
    """Apple careers (jobs.apple.com) — no public API, so scrape the server-rendered
    search results (job links + titles embedded for SEO), scoped to Media & Entertainment
    (Apple TV+, Music, Podcasts, News, Books, Arcade, Sports, Beats) via queries. Drops
    retail/eng/hardware at source; deep scoring keeps only core-function fits. A scrape,
    so fragile — returns [] rather than guessing if the markup changes."""
    name = ats = "apple"
    BASE = "https://jobs.apple.com/en-us/search"
    # job-row link: aria-label="<title> <id>" href="/en-us/details/<id>-<sub>/<slug>?team=<TEAM>"
    _JOB = re.compile(
        r'aria-label="(?P<title>[^"]+?)\s+\d{6,}"\s+href="/en-us/details/(?P<id>[\d-]+)/'
        r'(?P<slug>[^"?]+?)(?:\?team=(?P<team>[A-Za-z0-9]+))?"')
    # Apple team codes that are never M&E business roles: Apple Store/retail, hardware, ML/AI eng.
    DROP_TEAMS = {"APPST", "HRDWR", "MLAI"}
    _DROP = re.compile(
        r"\b(specialist|genius|store leader|technician|advisor|software engineer|hardware|silicon|"
        r"firmware|engineer|engineering|\bux\b|\bui\b|designer|scientist|machine learning|developer)\b", re.I)
    # REQUIRE a Media & Entertainment signal in the title (keeps Apple to M&E; drops
    # iCloud/Cloud/Pay/enterprise strategy roles that would otherwise pass on function).
    _ME = re.compile(
        r"\b(apple tv|tv\+|music|podcast|news|sport|book|arcade|beats|original|content|"
        r"entertainment|video|streaming|media|fitness)\b", re.I)
    MAX_PAGES = 12  # 20 jobs/page -> ~240 newest US roles scanned per run

    def __init__(self, slug=None, company="Apple", session=None, timeout: int = 25, **extra):
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()
        self.location = extra.get("location") or "United States"

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        """Crawl the newest-first US list page by page and filter to M&E business
        titles locally. (Since ~Jul 2026 Apple's server ignores search params — the
        SPA filters client-side — but plain pagination still returns distinct,
        newest-first server-rendered pages, so a crawl keeps working.)"""
        out, seen = {}, set()
        headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html"}
        for page in range(1, self.MAX_PAGES + 1):
            params = {"location": "united-states-USA", "sort": "newest", "page": page}
            try:
                resp = self.session.get(self.BASE, params=params, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException:
                break
            new_ids = 0
            for m in self._JOB.finditer(resp.text):
                jid, title, team = m.group("id"), html.unescape(m.group("title")).strip(), (m.group("team") or "")
                if jid in seen:
                    continue
                seen.add(jid)
                new_ids += 1
                if team in self.DROP_TEAMS or self._DROP.search(title) or not self._ME.search(title):
                    continue  # M&E business roles only
                out[jid] = self._normalize(jid, m.group("slug"), title, team)
            if new_ids == 0:  # ran out of fresh pages
                break
        return list(out.values())

    def _normalize(self, jid: str, slug: str, title: str, team: str) -> Job:
        return Job(
            source=self.ats, source_job_id=jid, title=title, company=self.company,
            location=self.location, remote=_remote_from_text(title),
            description=f"{title}. Apple Careers role ({self.location}); team {team}.",
            url=f"https://jobs.apple.com/en-us/details/{jid}/{slug}",
            category=team, posted_at=None, raw={"id": jid, "slug": slug, "team": team},
        )


class SnapSource(JobSource):
    """Snap careers (careers.snap.com) — their own JSON API over the Workday backend.
    Returns an Elasticsearch projection per job (title / departments / offices / URL;
    no JD text, so triage scores on the title). The API ignores text-search params,
    so fetch the whole list (~150-200 roles) and let the location filter + scoring
    narrow it. Missing fields stay None — never fabricated."""
    name = ats = "snap"
    URL = "https://careers.snap.com/api/jobs"

    def __init__(self, slug=None, company="Snap Inc.", session=None, timeout: int = 25, **extra):
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}
        resp = self.session.get(self.URL, params={"limit": 500}, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        out: List[Job] = []
        for hit in (resp.json().get("body") or []):
            src = hit.get("_source") or {}
            title = (src.get("title") or "").strip()
            url = src.get("absolute_url")
            jid = str(src.get("id") or hit.get("_id") or "")
            if not (title and url and jid):
                continue
            offices = src.get("offices") or []
            locs = sorted({(o.get("location") or o.get("city") or "").strip()
                           for o in offices if isinstance(o, dict)} - {""})
            location = ", ".join(locs) or (src.get("primary_location") or None)
            dept = src.get("departments")
            out.append(Job(
                source=self.ats, source_job_id=jid, title=title, company=self.company,
                location=location, remote=_remote_from_text(f"{title} {location or ''}"),
                description=None, url=url,
                category=dept if isinstance(dept, str) else None,
                raw={"departments": dept, "offices": offices,
                     "employment_type": src.get("employment_type")},
            ))
        return out


class BambooHRSource(JobSource):
    """BambooHR hosted careers ({slug}.bamboohr.com) — public JSON at /careers/list.
    List records are sparse (title / department / location; no JD text), so triage
    scores the title. Detail URL: /careers/<id>. Missing fields stay None."""
    name = ats = "bamboohr"

    def __init__(self, slug: str, company: str, session=None, timeout: int = 20, **extra):
        self.slug = slug
        self.company = company
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, query: Optional[JobQuery] = None) -> List[Job]:
        url = f"https://{self.slug}.bamboohr.com/careers/list"
        resp = self.session.get(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
                                timeout=self.timeout)
        resp.raise_for_status()
        out: List[Job] = []
        for row in (resp.json().get("result") or []):
            jid = str(row.get("id") or "")
            title = (row.get("jobOpeningName") or "").strip()
            if not (jid and title):
                continue
            loc = row.get("location")
            if isinstance(loc, dict):
                location = ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p) or None
            else:
                location = str(loc).strip() or None if loc else None
            dept = row.get("departmentLabel") or row.get("departmentId")
            remote = (True if str(row.get("isRemote") or "").lower() in ("1", "true", "yes")
                      else _remote_from_text(location, title))
            out.append(Job(
                source=self.ats, source_job_id=jid, title=title, company=self.company,
                location=location, remote=remote, description=None,
                url=f"https://{self.slug}.bamboohr.com/careers/{jid}",
                category=str(dept) if dept is not None else None, raw=row,
            ))
        return out


SOURCE_BY_ATS = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
    "smartrecruiters": SmartRecruitersSource,
    "workday": WorkdaySource,
    "google": GoogleSource,
    "netflix": NetflixSource,
    "apple": AppleSource,
    "snap": SnapSource,
    "bamboohr": BambooHRSource,
}
