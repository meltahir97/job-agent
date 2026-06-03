"""The single seam between this app and claude-agent-sdk.

Every model call in the project goes through here, configured for grounding:
  * allowed_tools=[] and explicit disallowed web/file tools -> the model cannot
    browse, fetch, or read anything. It sees only the text we put in the prompt.
  * setting_sources=[] -> no filesystem settings / CLAUDE.md leak into context.
  * max_turns=1 -> a single response, no agentic loops.

Centralizing this also makes the reasoning layer trivially mockable in tests.
"""
from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Optional

from .. import config


class LLMError(RuntimeError):
    pass


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def parse_json(text: str) -> Any:
    """Best-effort extraction of a JSON value (object or array) from model output."""
    s = _strip_fences(text)
    try:
        return json.loads(s)
    except Exception:
        pass
    starts = [p for p in (s.find("{"), s.find("[")) if p != -1]
    if starts:
        start = min(starts)
        end = max(s.rfind("}"), s.rfind("]"))
        if end > start:
            try:
                return json.loads(s[start : end + 1])
            except Exception:
                pass
    raise LLMError(f"Model did not return valid JSON. First 200 chars: {text[:200]!r}")


async def _aquery(prompt: str, *, model: str, system: str, max_budget_usd: Optional[float]) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        allowed_tools=[],  # grounding: no tools at all
        disallowed_tools=["WebSearch", "WebFetch", "Bash", "Read", "Edit", "Write"],
        max_turns=1,
        setting_sources=[],  # hermetic: ignore project/user settings
        max_budget_usd=max_budget_usd,
    )
    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)


def complete_text(prompt: str, *, model: str, system: str, max_budget_usd: Optional[float] = None) -> str:
    """Run a single grounded, tool-free completion and return the raw text."""
    import anyio

    if not config.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY not set (add it to .env).")
    # Ensure the key reaches the CLI subprocess even if the ambient var was blank.
    os.environ["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY
    try:
        return anyio.run(
            functools.partial(
                _aquery, prompt, model=model, system=system, max_budget_usd=max_budget_usd
            )
        )
    except LLMError:
        raise
    except Exception as e:  # surface SDK/transport/billing errors cleanly
        raise LLMError(str(e)) from e


def complete_json(prompt: str, *, model: str, system: str, max_budget_usd: Optional[float] = None) -> Any:
    """Run a completion and parse its output as JSON (object or array)."""
    return parse_json(complete_text(prompt, model=model, system=system, max_budget_usd=max_budget_usd))
