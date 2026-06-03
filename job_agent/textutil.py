"""Tiny dependency-free helpers for normalizing ATS payloads."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Optional

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</\s*(p|div|li|ul|ol|h[1-6]|tr)\s*>", re.I)
_BR = re.compile(r"<\s*br\s*/?>", re.I)


def html_to_text(s: Optional[str]) -> Optional[str]:
    """Convert an HTML description fragment to readable plain text. Returns None if empty."""
    if not s:
        return None
    text = html.unescape(s)            # decode entities (handles escaped markup too)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)         # second pass for double-escaped content
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def epoch_ms_to_iso(ms) -> Optional[str]:
    """Milliseconds-since-epoch -> ISO-8601 UTC. None on bad input (never guesses)."""
    try:
        return (
            datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None
