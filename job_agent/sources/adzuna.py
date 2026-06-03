"""AdzunaSource — the first concrete JobSource.

Plain Python, no LLM. Calls the Adzuna Jobs search API and normalizes each result
into a `Job`. Grounding rules:
  * Only fields actually present in the payload are mapped; missing -> None.
  * Adzuna *predicted* salaries (salary_is_predicted == "1") are estimates, not
    employer-stated figures, so they are dropped to null (the full payload is still
    retained in raw_json). We never present a guessed salary as real.
"""
from __future__ import annotations

import time
from typing import List, Optional

import requests

from ..models import Job
from .base import JobQuery, JobSource

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"


class AdzunaConfigError(RuntimeError):
    """Raised when Adzuna credentials are missing."""


class AdzunaSource(JobSource):
    name = "adzuna"

    def __init__(
        self,
        app_id: Optional[str],
        app_key: Optional[str],
        country: str = "us",
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ) -> None:
        if not app_id or not app_key:
            raise AdzunaConfigError(
                "Adzuna credentials missing (set ADZUNA_APP_ID and ADZUNA_APP_KEY)."
            )
        self.app_id = app_id
        self.app_key = app_key
        self.country = country
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, query: JobQuery) -> List[Job]:
        out: List[Job] = []
        per_page = min(max(query.max_results, 1), 50)  # Adzuna caps at 50/page
        pages = (query.max_results + per_page - 1) // per_page
        for page in range(1, pages + 1):
            payload = self._request(query, page, per_page)
            batch = payload.get("results") or []
            out.extend(self._normalize(raw) for raw in batch)
            if len(batch) < per_page or len(out) >= query.max_results:
                break
            time.sleep(0.25)  # be polite to the API
        return out[: query.max_results]

    def _request(self, query: JobQuery, page: int, per_page: int) -> dict:
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": per_page,
            "content-type": "application/json",
        }
        if query.keywords:
            params["what"] = query.keywords
        if query.location:
            params["where"] = query.location
        if query.max_days_old:
            params["max_days_old"] = query.max_days_old
        for k, v in (query.extra or {}).items():  # source-specific knobs (what_or, ...)
            params[k] = v
        url = f"{ADZUNA_BASE}/{self.country}/search/{page}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _normalize(self, raw: dict) -> Job:
        company = (raw.get("company") or {}).get("display_name")
        location = (raw.get("location") or {}).get("display_name")
        category = (raw.get("category") or {}).get("label")

        predicted = str(raw.get("salary_is_predicted", "0")) == "1"
        salary_min = None if predicted else raw.get("salary_min")
        salary_max = None if predicted else raw.get("salary_max")
        currency = "USD" if (self.country == "us" and (salary_min or salary_max)) else None

        return Job(
            source=self.name,
            source_job_id=str(raw.get("id")) if raw.get("id") is not None else "",
            title=raw.get("title"),
            company=company,
            location=location,
            remote=self._detect_remote(raw, location),
            description=raw.get("description"),
            url=raw.get("redirect_url"),
            salary_min=float(salary_min) if salary_min is not None else None,
            salary_max=float(salary_max) if salary_max is not None else None,
            salary_currency=currency,
            category=category,
            contract_type=raw.get("contract_type"),
            posted_at=raw.get("created"),  # already ISO-8601
            raw=raw,
        )

    @staticmethod
    def _detect_remote(raw: dict, location: Optional[str]) -> Optional[bool]:
        """True only if the listing explicitly signals remote; else None (unknown).

        We never assert False — absence of the word doesn't prove on-site.
        """
        hay = " ".join(
            x for x in (raw.get("title"), location, raw.get("description")) if x
        ).lower()
        if "remote" in hay or "work from home" in hay:
            return True
        return None
