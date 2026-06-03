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
                src = source_cls(res.slug, co.name, session=self.session, timeout=self.timeout)
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
