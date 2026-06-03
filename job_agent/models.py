"""Core data structures shared across layers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class Job:
    """A normalized job listing.

    Fields map 1:1 to the `jobs` table. Anything a source does not provide stays
    None — the data layer must never fabricate values.
    """

    source: str
    source_job_id: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    remote: Optional[bool] = None
    description: Optional[str] = None
    url: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    category: Optional[str] = None
    contract_type: Optional[str] = None
    posted_at: Optional[str] = None  # ISO-8601
    raw: dict = field(default_factory=dict)  # original source payload (provenance)

    @property
    def fingerprint(self) -> str:
        """Stable hash of normalized title|company|location for cross-source dedup."""
        basis = "|".join((_norm(self.title), _norm(self.company), _norm(self.location)))
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()
