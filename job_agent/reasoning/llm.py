"""The single seam between this app and Claude — via the raw Anthropic Messages API.

Why the raw API (not claude-agent-sdk): the agent SDK spawned a Claude Code CLI
subprocess per call (~25-30s overhead each), making a full scoring run ~35 minutes.
The Messages API has no per-call process overhead and is safe to call concurrently.

Grounding is inherent: we pass NO tools, so the model cannot browse, fetch, or
invent — it sees only the data we put in the prompt. Calls are single-shot.

Concurrency: `map_json` runs batches with an asyncio semaphore (default cap 5) over
AsyncAnthropic; the SDK auto-retries 429/5xx with backoff (max_retries). An optional
`batch=True` path uses the Message Batches API (~50% cheaper, latency-tolerant) for
scheduled runs. Prompt caching: the stable instructions+profile prefix is sent as a
cached `system` block, reused across every batch.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
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


def _system_blocks(system: str, cache: bool):
    block = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text")


# --- single, synchronous call (used by one-off resume profiling) ------------

def complete_text(prompt: str, *, model: str, system: str, max_tokens: int = 4096, cache: bool = True) -> str:
    import anthropic

    client = _get_client()
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=_system_blocks(system, cache),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise LLMError(_error_text(e)) from e
    return _text_of(resp)


def complete_json(prompt: str, *, model: str, system: str, max_tokens: int = 4096, cache: bool = True) -> Any:
    return parse_json(complete_text(prompt, model=model, system=system, max_tokens=max_tokens, cache=cache))


# --- web-search-grounded call (server-side tool; used by company discovery) --

def web_search(prompt: str, *, model: str, system: str, max_tokens: int = 4096, max_searches: int = 5):
    """Run a message with Anthropic's server-side web_search tool enabled.

    Returns (final_text, cited_urls). The model browses to ground its answer; the
    caller still independently VERIFIES anything actionable (resolver probe / HTTP),
    so a hallucinated URL can never become a proposal.
    """
    import anthropic

    client = _get_client()
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=_system_blocks(system, cache=False),
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise LLMError(_error_text(e)) from e
    return _text_of(resp), _cited_urls(resp)


def _cited_urls(message) -> List[str]:
    """Pull the real URLs the web_search tool actually returned (for cross-checking)."""
    urls: List[str] = []
    for block in getattr(message, "content", []) or []:
        # citations attached to answer text
        for c in getattr(block, "citations", None) or []:
            u = getattr(c, "url", None)
            if u:
                urls.append(u)
        # raw web_search_tool_result blocks
        content = getattr(block, "content", None)
        if isinstance(content, list):
            for item in content:
                u = getattr(item, "url", None)
                if u:
                    urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --- concurrent batch scoring (Messages API, asyncio + semaphore) -----------

async def _amap(prompts, *, model, system, max_tokens, cache, concurrency) -> List[Optional[Any]]:
    import anthropic

    sblocks = _system_blocks(system, cache)
    async with anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4) as client:
        sem = asyncio.Semaphore(concurrency)

        async def call(prompt: str):
            async with sem:
                resp = await client.messages.create(
                    model=model, max_tokens=max_tokens, system=sblocks,
                    messages=[{"role": "user", "content": prompt}],
                )
                return parse_json(_text_of(resp))

        async def safe(prompt: str):
            try:
                return await call(prompt)
            except Exception:
                return None  # per-batch failure -> caller fails open

        # Warm the shared cache (and surface systemic errors) with the first call,
        # then fan the rest out across the semaphore.
        first = await call(prompts[0])
        rest = await asyncio.gather(*(safe(p) for p in prompts[1:]))
        return [first, *rest]


def map_json(
    prompts: List[str],
    *,
    model: str,
    system: str,
    max_tokens: int = 4096,
    cache: bool = True,
    concurrency: int = 5,
    batch: bool = False,
) -> List[Optional[Any]]:
    if not prompts:
        return []
    if not config.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY not set (add it to .env).")
    if batch:
        return _map_json_batch(prompts, model=model, system=system, max_tokens=max_tokens, cache=cache)

    import anthropic

    try:
        return asyncio.run(_amap(prompts, model=model, system=system, max_tokens=max_tokens, cache=cache, concurrency=concurrency))
    except anthropic.APIError as e:
        raise LLMError(_error_text(e)) from e


# --- optional Batch API path (~50% cheaper; latency-tolerant) ---------------

def _map_json_batch(prompts, *, model, system, max_tokens, cache, poll_seconds: int = 20) -> List[Optional[Any]]:
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = _get_client()
    sblocks = _system_blocks(system, cache)
    requests = [
        Request(
            custom_id=f"b{i}",
            params=MessageCreateParamsNonStreaming(
                model=model, max_tokens=max_tokens, system=sblocks,
                messages=[{"role": "user", "content": p}],
            ),
        )
        for i, p in enumerate(prompts)
    ]
    try:
        batch = client.messages.batches.create(requests=requests)
        while client.messages.batches.retrieve(batch.id).processing_status != "ended":
            time.sleep(poll_seconds)
        out: List[Optional[Any]] = [None] * len(prompts)
        for result in client.messages.batches.results(batch.id):
            if result.result.type == "succeeded":
                try:
                    out[int(result.custom_id[1:])] = parse_json(_text_of(result.result.message))
                except (LLMError, ValueError):
                    pass
        return out
    except anthropic.APIError as e:
        raise LLMError(_error_text(e)) from e
