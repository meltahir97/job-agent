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
    # NOTE: no bare-first-word candidate — "Thrive Fantasy" must never match some other
    # company's live "thrive" board. Full-name joins only; identity re-checked below.
    for c in ("".join(words), "".join(core), "-".join(core), "-".join(words)):
        c = c.strip("-")
        if c and c not in out:
            out.append(c)
    return out[:4]


def _norm_join(s: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", s or "")  # "(Sony)", "(formerly Wikia)" are annotations
    return "".join(w for w in re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()
                   if w not in _SUFFIXES)


def _same_company(display_name: str, board_name: Optional[str]) -> bool:
    """Ownership gate: does the board's own display name plausibly belong to this
    company? One normalized name must contain the other and lengths must be close —
    'thrivefantasy' vs 'thrive' fails; 'crunchyrollsony' vs 'crunchyroll' passes."""
    if not board_name:
        return True  # board exposes no name — nothing to check against
    a, b = _norm_join(display_name), _norm_join(board_name)
    if not a or not b:
        return True
    if a not in b and b not in a:
        return False
    return min(len(a), len(b)) / max(len(a), len(b)) >= 0.6


def _greenhouse_board_name(slug: str, session, timeout: int) -> Optional[str]:
    try:
        r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                        timeout=timeout, headers={"User-Agent": "job-agent/0.1 (board resolver)"})
        if r.status_code == 200:
            return (r.json().get("name") or "").strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


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
            # Workable's public endpoint returns 200+empty for ANY account, so an empty
            # board is indistinguishable from "no board" — only trust it when n > 0.
            if n is not None and (ats != "workable" or n > 0):
                if ats == "greenhouse" and not _same_company(
                        company.name, _greenhouse_board_name(slug, session, timeout)):
                    continue  # live board, but it belongs to a DIFFERENT company
                return Resolution(
                    company.name, ats, slug, "resolved", n, f"{ats}:{slug} ({n} open roles)",
                )
    return Resolution(
        company.name, None, None, "unresolved",
        detail="no public Greenhouse/Lever/Ashby/Workable board matched; supply ats+slug manually",
    )
