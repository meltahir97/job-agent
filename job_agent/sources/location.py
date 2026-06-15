"""The Bay-Area / US-remote location filter.

Uses a POSITIVE US-signal (US state names/abbreviations, "United States",
"Remote - US", etc.) alongside the Bay allow-list and a non-US deny-list:

  * Bay Area                         -> KEEP
  * Remote, not explicitly non-US    -> KEEP (remote=True)
  * Remote but explicitly non-US     -> DROP
  * Clearly non-US (onsite)          -> DROP
  * US but clearly not Bay (onsite)  -> DROP   (non-CA US state, or a known non-Bay US city)
  * Genuinely unparseable            -> KEEP with remote=None  (never guess a location)

Criteria live in config.*_TERMS / *_TOKENS / US_STATE_* — tune them there.
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


def _state_abbrs(original: Optional[str]) -> Set[str]:
    # Uppercase 2-letter tokens only — avoids matching lowercase words like "in"/"or".
    return set(re.findall(r"\b([A-Z]{2})\b", original or "")) & config.US_STATE_ABBR


def location_decision(location: Optional[str], remote: Optional[bool] = None) -> LocationDecision:
    loc = _norm(location)
    remote_signal = (remote is True) or _match(loc, config.REMOTE_TERMS, set())

    if _match(loc, config.BAY_AREA_TERMS, config.BAY_AREA_TOKENS):
        return LocationDecision(True, True if remote_signal else None, "Bay Area")

    non_us = _match(loc, config.NON_US_TERMS, config.NON_US_TOKENS)
    state_names = [s for s in config.US_STATE_NAMES if s in loc]
    state_abbrs = _state_abbrs(location)
    us_signal = (
        bool(state_names or state_abbrs)
        or _match(loc, config.US_TERMS, config.US_TOKENS)
        or any(t in loc for t in config.US_REMOTE_TERMS)
    )

    if remote_signal:
        if non_us and not us_signal:
            return LocationDecision(False, None, "remote but explicitly non-US")
        return LocationDecision(True, True, "remote (US/unspecified)")

    if non_us:
        return LocationDecision(False, None, "clearly non-US location")

    # US but not Bay, on-site: a non-California US state, or a known non-Bay US city.
    non_ca_state = bool([s for s in state_names if s != "california"] or (state_abbrs - {"CA"}))
    if non_ca_state or _match(loc, config.US_NON_BAY_TERMS, set()):
        return LocationDecision(False, None, "US location outside the Bay Area")

    if not loc:
        return LocationDecision(True, None, "location unspecified (ambiguous)")
    return LocationDecision(True, None, "ambiguous location (kept)")
