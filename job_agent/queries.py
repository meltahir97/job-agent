"""Default search query set built from your parameters in config.

Manager/Director/VP-level roles in Strategy, Operations, Business Development, and
Corporate Development, in the Bay Area or Remote.

Note: Adzuna has no true "remote" filter, so remote intent is biased via keywords
and confirmed later by `AdzunaSource._detect_remote` + the reasoning layer. A
dedicated remote-friendly source is a planned extension.
"""
from __future__ import annotations

from typing import List

from . import config
from .sources.base import JobQuery

# Adzuna `what_or` matches ANY of these words — biases results toward senior titles.
SENIORITY_OR = "manager director vp president head"


def default_queries(max_results: int = 50, max_days_old: int = 30) -> List[JobQuery]:
    queries: List[JobQuery] = []
    for domain in config.TARGET_DOMAINS:
        # Bay Area
        queries.append(
            JobQuery(
                keywords=domain,
                location="San Francisco",
                max_results=max_results,
                max_days_old=max_days_old,
                extra={"what_or": SENIORITY_OR},
            )
        )
        # Remote (keyword-biased; see module note)
        queries.append(
            JobQuery(
                keywords=f"remote {domain}",
                location=None,
                remote=True,
                max_results=max_results,
                max_days_old=max_days_old,
                extra={"what_or": SENIORITY_OR},
            )
        )
    return queries
