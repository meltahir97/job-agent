"""Output tiers shared by the digest and the website.

Thresholds live in config (TIER_STRONG_MIN / TIER_LOOK_MIN) so they're tunable in
one place. 'skip'-labelled and null/below-threshold roles are excluded (tier=None).
"""
from __future__ import annotations

from typing import Optional

from . import config

STRONG = "strong"
LOOK = "look"
TIER_TITLES = {STRONG: "Strong matches", LOOK: "Worth a look"}
TIER_BADGES = {STRONG: "⭐", LOOK: "🔭"}
ORDER = (STRONG, LOOK)


def tier_for(fit_score: Optional[int], label: Optional[str]) -> Optional[str]:
    if label == "skip" or fit_score is None:
        return None
    if fit_score >= config.TIER_STRONG_MIN:
        return STRONG
    if fit_score >= config.TIER_LOOK_MIN:
        return LOOK
    return None
