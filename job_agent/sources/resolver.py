"""Resolve a watchlist company to a concrete (ats, slug) board.

For ats != auto we trust the config. For ats == auto we derive candidate slugs
from the company name and probe the public ATS endpoints; the first board that
actually responds wins. If nothing responds we mark the company UNRESOLVED and
never guess — the caller surfaces these so the user can supply ats+slug manually.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ..companies import Company
from . import ats as ats_mod

# Words dropped when guessing a slug from a display name.
_SUFFIXES = {
    "inc", "llc", "ltd", "corp", "co", "labs", "technologies", "technology",
    "software", "systems", "group", "holdings", "the", "company",
}


@dataclass
class Resolution:
    company: str
    ats: Optional[str]
    slug: Optional[str]
    status: str  # "configured" | "resolved" | "unresolved"
    n_jobs: Optional[int] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("configured", "resolved")


def candidate_slugs(name: str) -> List[str]:
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split() if w]
    core = [w for w in words if w not in _SUFFIXES] or words
    out: List[str] = []
    for c in ("".join(words), "".join(core), "-".join(core), "-".join(words), core[0] if core else ""):
        c = c.strip("-")
        if c and c not in out:
            out.append(c)
    return out[:4]


def resolve_company(company: Company, session, *, atses: Optional[List[str]] = None, timeout: int = 10) -> Resolution:
    if company.ats != "auto":
        return Resolution(
            company.name, company.ats, company.slug, "configured",
            detail=f"{company.ats}:{company.slug} (from config)",
        )
    atses = atses or ats_mod.ATS_NAMES
    for slug in candidate_slugs(company.name):
        for ats in atses:
            n = ats_mod.probe(ats, slug, session, timeout)
            if n is not None:
                return Resolution(
                    company.name, ats, slug, "resolved", n, f"{ats}:{slug} ({n} open roles)",
                )
    return Resolution(
        company.name, None, None, "unresolved",
        detail="no public Greenhouse/Lever/Ashby/Workable board matched; supply ats+slug manually",
    )
