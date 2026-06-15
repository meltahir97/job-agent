"""The single seam between this app and Claude — via the raw Anthropic Messages API.

Why the raw API (not claude-agent-sdk): the agent SDK spawns a Claude Code CLI
subprocess per call (~25-30s overhead each), which made a full scoring run take
~35 minutes. The Messages API has no per-call process overhead and is safe to call
concurrently, cutting a full run to a couple of minutes.

Grounding is inherent here: we pass NO tools, so the model cannot browse, fetch, or
invent — it sees only the data we put in the prompt. Calls are single-shot.

Prompt caching: the stable prefix (instructions + candidate profile) is passed as a
cached `system` block, reused across every batch in a run; only the per-batch jobs
vary in the user message.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from .. import config


class LLMError(RuntimeError):
    pass


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic

        if not config.ANTHROPIC_API_KEY:
            raise LLMError("ANTHROPIC_API_KEY not set (add it to .env).")
        # Explicit key: the harness may export a blank ANTHROPIC_API_KEY that would
        # otherwise shadow the real one. max_retries covers transient 429/5xx.
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4)
    return _client


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


def _error_text(e) -> str:
    msg = getattr(e, "message", None) or str(e)
    low = msg.lower()
    if "credit" in low or "balance" in low:
        msg += " — add credit at https://console.anthropic.com/settings/billing"
    return msg


def complete_text(prompt: str, *, model: str, system: str, max_tokens: int = 4096, cache: bool = True) -> str:
    """One grounded, tool-free completion. `system` is sent as a cacheable prefix."""
    import anthropic

    client = _get_client()
    system_blocks = [{"type": "text", "text": system}]
    if cache:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise LLMError(_error_text(e)) from e
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def complete_json(prompt: str, *, model: str, system: str, max_tokens: int = 4096, cache: bool = True) -> Any:
    return parse_json(complete_text(prompt, model=model, system=system, max_tokens=max_tokens, cache=cache))


def map_json(
    prompts: List[str],
    *,
    model: str,
    system: str,
    max_tokens: int = 4096,
    workers: int = 6,
    cache: bool = True,
) -> List[Optional[Any]]:
    """Run many batches that share the same cached `system` prefix, concurrently.

    The first call runs alone to warm the shared cache; the rest fan out across a
    thread pool (so they read the cached prefix). A per-batch failure yields None
    (the caller fails open) rather than aborting the whole run; a systemic failure
    (bad key, no credit) surfaces immediately from the warm-up call.
    """
    if not prompts:
        return []

    results: List[Optional[Any]] = [None] * len(prompts)
    results[0] = complete_json(prompts[0], model=model, system=system, max_tokens=max_tokens, cache=cache)

    rest = list(range(1, len(prompts)))
    if rest:
        def one(i: int):
            try:
                return complete_json(prompts[i], model=model, system=system, max_tokens=max_tokens, cache=cache)
            except LLMError:
                return None  # transient/per-batch failure → caller handles missing results

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, res in zip(rest, ex.map(one, rest)):
                results[i] = res
    return results
