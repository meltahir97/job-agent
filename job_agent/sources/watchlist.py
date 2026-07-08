"""WatchlistSource — orchestrates per-company ATS polling.

For each company in the watchlist it resolves the board, fetches via the matching
ATS source, applies the location filter, and collects survivors. It also returns a
report so the caller can show resolved / unresolved / errored companies. Pure data
layer: no LLM, no persistence (the caller upserts via store).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

from ..models import Job
from . import ats as ats_http
from . import ats_sources, resolver
from .location import location_decision
from .resolver import Resolution


@dataclass
class CompanyResult:
    company: str
    resolution: Resolution
    fetched: int = 0
    kept: int = 0
    error: Optional[str] = None


@dataclass
class WatchlistReport:
    results: List[CompanyResult] = field(default_factory=list)

    @property
    def unresolved(self) -> List[CompanyResult]:
        return [r for r in self.results if r.resolution.status == "unresolved"]

    @property
    def errored(self) -> List[CompanyResult]:
        return [r for r in self.results if r.error]

    @property
    def fetched_ok(self) -> List[CompanyResult]:
        return [r for r in self.results if r.error is None and r.resolution.ok]


def audit_watchlist(companies, session=None, retries: int = 1) -> List[dict]:
    """Health-check every watchlist entry: does its feed actually return roles? For a
    broken Greenhouse/Lever/Ashby/Workable slug, auto-resolve the correct one (so the
    user never hand-debugs slugs). Returns one result dict per company:
    {company, ats, slug, ok, count, detail, fix}. `fix` is a suggested 'ats:slug' or None.
    """
    from ..companies import Company

    session = session or requests.Session()
    results = []
    for co in companies:
        ats, slug, ok, count, detail, fix = co.ats, co.slug, False, 0, "", None
        try:
            if co.ats == "auto":  # board not pinned yet — try to resolve it right now
                r = resolver.resolve_company(Company(co.name, "auto"), session)
                if r.ok and r.slug:
                    ok, count = True, r.n_jobs or 0
                    fix = f"{r.ats}:{r.slug}"
                    detail = f"auto-resolves to {r.ats}:{r.slug}"
                else:
                    detail = "no public job board found yet (auto — re-tried every run)"
            elif co.ats in ("google", "apple", "netflix", "snap"):  # custom sources — 1 query = quick check
                ex = dict(co.extra); ex["queries"] = (co.extra.get("queries") or [])[:1]
                count = len(ats_sources.SOURCE_BY_ATS[co.ats](co.slug, co.name, session=session, timeout=25, **ex).fetch())
                ok, detail = count > 0, "custom source"
            elif co.ats in ("greenhouse", "lever", "ashby", "workable"):
                n = None
                for _ in range(retries + 1):  # retry to absorb transient probe failures
                    n = ats_http.probe(co.ats, co.slug, session, 12)
                    if n:
                        break
                if n:
                    ok, count, detail = True, n, "ok"
                else:
                    r = resolver.resolve_company(Company(co.name, "auto"), session)
                    detail = "feed returned nothing"
                    if r.ok and r.slug:
                        fix, detail = f"{r.ats}:{r.slug}", f"auto-resolved to {r.ats}:{r.slug} ({r.n_jobs} roles)"
            else:  # workday / smartrecruiters
                count = len(ats_sources.SOURCE_BY_ATS[co.ats](co.slug, co.name, session=session, timeout=25, **co.extra).fetch())
                ok, detail = count > 0, "ok"
        except Exception as e:  # noqa: BLE001
            detail = f"error: {str(e)[:80]}"
        results.append({"company": co.name, "ats": ats, "slug": slug, "ok": ok,
                        "count": count, "detail": detail, "fix": fix})
    return results


class WatchlistSource:
    def __init__(self, companies, session=None, timeout: int = 20):
        self.companies = companies
        self.session = session or requests.Session()
        self.timeout = timeout

    def collect(self) -> Tuple[List[Job], WatchlistReport]:
        jobs: List[Job] = []
        report = WatchlistReport()

        for co in self.companies:
            res = resolver.resolve_company(co, self.session, timeout=min(self.timeout, 10))
            cr = CompanyResult(co.name, res)

            if not res.ok:
                report.results.append(cr)
                continue

            source_cls = ats_sources.SOURCE_BY_ATS.get(res.ats)
            if source_cls is None:
                cr.error = f"no source for ats={res.ats}"
                report.results.append(cr)
                continue

            try:
                src = source_cls(res.slug, co.name, session=self.session, timeout=self.timeout, **(co.extra or {}))
                fetched = src.fetch()
            except Exception as e:  # network/HTTP/schema — record and keep going
                cr.error = f"fetch failed: {e}"
                report.results.append(cr)
                continue

            cr.fetched = len(fetched)
            for job in fetched:
                decision = location_decision(job.location, job.remote)
                if decision.keep:
                    job.remote = decision.remote  # filter refines remote (True/None only)
                    jobs.append(job)
                    cr.kept += 1
            report.results.append(cr)

        return jobs, report
