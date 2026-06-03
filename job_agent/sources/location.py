"""The Bay-Area / US-remote location filter.

Decision (criteria live in config.*_TERMS / *_TOKENS, tune them there):
  * Bay Area                       -> KEEP
  * Remote incl. US/CA/unspecified -> KEEP (remote=True)
  * Remote but explicitly non-US   -> DROP
  * Clearly non-US, or US-but-not-Bay onsite -> DROP
  * Anything ambiguous             -> KEEP with remote=None (never guess a location)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Set

from .. import config


@dataclass
class LocationDecision:
    keep: bool
    remote: Optional[bool]
    reason: str


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", s).strip().lower() if s else ""


def _match(loc: str, phrases, tokens: Set[str]) -> bool:
    if any(p in loc for p in phrases):
        return True
    words = set(re.findall(r"[a-z]+", loc))
    return bool(words & tokens)


def location_decision(location: Optional[str], remote: Optional[bool] = None) -> LocationDecision:
    loc = _norm(location)
    remote_signal = (remote is True) or _match(loc, config.REMOTE_TERMS, set())

    if _match(loc, config.BAY_AREA_TERMS, config.BAY_AREA_TOKENS):
        return LocationDecision(True, True if remote_signal else None, "Bay Area")

    if remote_signal:
        non_us = _match(loc, config.NON_US_TERMS, config.NON_US_TOKENS)
        us_incl = _match(loc, config.US_TERMS, config.US_TOKENS)
        if non_us and not us_incl:
            return LocationDecision(False, None, "remote but explicitly non-US")
        return LocationDecision(True, True, "remote (US/unspecified)")

    if not loc:
        return LocationDecision(True, None, "location unspecified (ambiguous)")
    if _match(loc, config.NON_US_TERMS, config.NON_US_TOKENS):
        return LocationDecision(False, None, "clearly non-US location")
    if _match(loc, config.US_NON_BAY_TERMS, set()):
        return LocationDecision(False, None, "US location outside the Bay Area")
    return LocationDecision(True, None, "ambiguous location (kept)")
