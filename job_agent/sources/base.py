"""The JobSource interface — the single extension point for new data sources.

Implementations (now: Adzuna; later: Greenhouse / Lever / Workable ATS feeds and a
paid aggregator) normalize their native payloads into `Job` records. They are pure
Python and make NO LLM calls. They must never fabricate fields: anything the source
omits stays None.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import Job


@dataclass
class JobQuery:
    """A normalized search request handed to any JobSource."""

    keywords: str                              # e.g. "Director Strategy"
    location: Optional[str] = None             # e.g. "San Francisco"; None = any/remote
    remote: Optional[bool] = None              # True to bias toward remote roles
    max_results: int = 50
    max_days_old: Optional[int] = None         # recency filter, if the source supports it
    extra: dict = field(default_factory=dict)  # source-specific knobs


class JobSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, query: JobQuery) -> List[Job]:
        """Fetch + normalize listings for a single query into Job records."""
        raise NotImplementedError
