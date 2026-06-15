"""Two-stage scoring: cheap triage (haiku) then deep scoring (sonnet/opus).

Tuned for RECALL: every listing is already from a company on the candidate's target
watchlist, so triage keeps anything the candidate is even close to qualified for and
only drops clear non-fits; deep scoring labels generously ('skip' only for roles with
no transferable overlap). The candidate profile lives in the cached system prefix so
it's reused across all batches; batches run concurrently (see llm.map_json).

Grounding holds: the model only ever sees the fields we pass it, results are matched
back by exact job id (invented ids ignored), and missing fields stay null.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .. import config, store
from . import llm

TRIAGE_SYSTEM = (
    "You are a fast, RECALL-ORIENTED job-fit triager for one specific candidate (profile "
    "below). Every listing is already from a company on the candidate's target watchlist, "
    "so the bar is intentionally LOW. KEEP any role the candidate could plausibly do OR "
    "stretch into given a background spanning corporate development, M&A, corporate strategy, "
    "strategic planning, business development, partnerships, operations / business operations, "
    "strategic finance, management consulting, and general management. When unsure, KEEP. "
    "Only DROP a role that clearly requires skills the candidate lacks or is plainly "
    "mismatched — hands-on software/ML engineering, deep technical or scientific "
    "specialization, hardware, clinical/medical, individual-contributor quota-carrying sales, "
    "recruiting/HR, legal/counsel, accounting, or narrow back-office operations (facilities, "
    "payroll, logistics) — or roles far junior to a senior leader. Use ONLY the provided "
    "fields; never invent. Return ONLY a JSON array."
)

DEEP_SYSTEM = (
    "You are evaluating job fit for one specific candidate whose profile is given below. For "
    "EACH provided listing produce a 0-100 fit score, a 2-3 sentence rationale citing SPECIFIC "
    "overlap between the candidate's profile and the listing, any red flags evident in the "
    "listing, and a label. Labels (be generous on recall): 'match' = strong, on-target fit; "
    "'stretch' = the candidate is at least partially or aspirationally qualified (transferable "
    "skills, an adjacent function, or a seniority step up/down); 'skip' = ONLY for roles that "
    "are clearly NOT a fit (a fundamentally different function with no transferable overlap, or "
    "requiring skills the candidate lacks). When torn between stretch and skip, choose stretch. "
    "Use ONLY the provided data; never invent salary, requirements, or facts. Return ONLY a JSON array."
)

PROFILE_KEYS = [
    "name", "seniority", "years_experience", "domains", "skills",
    "industries", "target_titles", "dealbreakers", "nice_to_haves", "summary",
]


def _profile_brief(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {k: profile.get(k) for k in PROFILE_KEYS if k in profile}


def _system_with_profile(base: str, profile: Dict[str, Any]) -> str:
    return (
        base
        + "\n\nCANDIDATE PROFILE (evaluate against this; use only facts present here):\n"
        + json.dumps(_profile_brief(profile), indent=2, ensure_ascii=False)
    )


def _feedback_block(feedback: Optional[List[sqlite3.Row]]) -> str:
    if not feedback:
        return ""
    saved = [f"{r['title']} @ {r['company']}" for r in feedback if r["decision"] == "saved"]
    dismissed = [f"{r['title']} @ {r['company']}" for r in feedback if r["decision"] == "dismissed"]
    if not (saved or dismissed):
        return ""
    return (
        "\n\nCANDIDATE FEEDBACK (weight these preferences):\n"
        f"  SAVED (liked): {saved or 'none'}\n"
        f"  DISMISSED (not interested): {dismissed or 'none'}\n"
    )


def _job_payload(row: sqlite3.Row, *, desc_chars: int) -> Dict[str, Any]:
    salary = None
    if row["salary_min"] or row["salary_max"]:
        salary = {"min": row["salary_min"], "max": row["salary_max"], "currency": row["salary_currency"]}
    remote = None if row["remote"] is None else bool(row["remote"])
    return {
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "remote": remote,
        "salary": salary,
        "category": row["category"],
        "posted_at": row["posted_at"],
        "description": (row["description"] or "")[:desc_chars],
    }


def _chunks(seq: List[Any], n: int) -> List[List[Any]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def _index_results(result: Any) -> Dict[int, dict]:
    if result is None:
        return {}
    if isinstance(result, dict):
        result = result.get("results") or result.get("jobs") or [result]
    out: Dict[int, dict] = {}
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and "id" in item:
                try:
                    out[int(item["id"])] = item
                except (TypeError, ValueError):
                    continue
    return out


def _clamp_score(v: Any) -> Optional[int]:
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _label_from_score(score: Optional[int]) -> str:
    if score is None:
        return "stretch"  # recall: unknown -> surface for review, not skip
    if score >= 75:
        return "match"
    if score >= 45:
        return "stretch"
    return "skip"


def _triage_user(jobs: List[dict]) -> str:
    return (
        "JOB LISTINGS (decide for each, by its exact id):\n"
        f"{json.dumps(jobs, indent=2, ensure_ascii=False)}\n\n"
        'Return ONLY a JSON array: [{"id": <int exactly as given>, "keep": <true|false>, '
        '"reason": "<short phrase>"}]. Default to keep when uncertain.'
    )


def _deep_user(jobs: List[dict]) -> str:
    return (
        "JOB LISTINGS (evaluate each, by its exact id):\n"
        f"{json.dumps(jobs, indent=2, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON array, one object per listing:\n"
        '[{"id": <int exactly as given>, "fit_score": <int 0-100>, '
        '"label": "match"|"stretch"|"skip", "rationale": "<2-3 sentences citing specific overlap>", '
        '"red_flags": ["<concerns evident in the listing>"]}]'
    )


def triage(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    rows: List[sqlite3.Row],
    *,
    model: str = config.TRIAGE_MODEL,
    batch_size: int = 25,
) -> int:
    system = _system_with_profile(TRIAGE_SYSTEM, profile)
    batches = _chunks(rows, batch_size)
    prompts = [_triage_user([_job_payload(r, desc_chars=600) for r in b]) for b in batches]
    results = llm.map_json(prompts, model=model, system=system, max_tokens=4096)

    kept = 0
    for batch, result in zip(batches, results):
        decided = _index_results(result)
        for r in batch:
            d = decided.get(r["id"])
            keep = True if d is None else bool(d.get("keep", True))  # fail open / default keep
            store.record_score(conn, r["id"], stage="triage", model=model, keep=keep,
                               rationale=(d or {}).get("reason"))
            kept += int(keep)
    return kept


def deep_score(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    rows: List[sqlite3.Row],
    *,
    model: str = config.DEEP_MODEL,
    batch_size: int = 8,
    feedback: Optional[List[sqlite3.Row]] = None,
) -> int:
    system = _system_with_profile(DEEP_SYSTEM, profile) + _feedback_block(feedback)
    batches = _chunks(rows, batch_size)
    prompts = [_deep_user([_job_payload(r, desc_chars=2000) for r in b]) for b in batches]
    results = llm.map_json(prompts, model=model, system=system, max_tokens=8192)

    n = 0
    for batch, result in zip(batches, results):
        decided = _index_results(result)
        for r in batch:
            d = decided.get(r["id"])
            if not d:  # omitted by model or failed batch — surface for review, never fabricate
                store.record_score(conn, r["id"], stage="deep", model=model, fit_score=None,
                                   label="stretch", rationale="Scoring did not complete; review manually.")
                continue
            score = _clamp_score(d.get("fit_score"))
            label = d.get("label") if d.get("label") in ("match", "stretch", "skip") else _label_from_score(score)
            red = d.get("red_flags")
            store.record_score(conn, r["id"], stage="deep", model=model, fit_score=score, label=label,
                               rationale=d.get("rationale"), red_flags=red if isinstance(red, list) else None)
            n += 1
    return n


def run_scoring(conn: sqlite3.Connection, profile: Dict[str, Any], *, deep_model: str = config.DEEP_MODEL) -> Dict[str, int]:
    to_triage = store.jobs_needing_triage(conn)
    kept = triage(conn, profile, to_triage) if to_triage else 0
    to_deep = store.jobs_needing_deep(conn)
    feedback = store.feedback_examples(conn)
    deep = deep_score(conn, profile, to_deep, model=deep_model, feedback=feedback) if to_deep else 0
    return {"triaged": len(to_triage), "kept": kept, "deep_scored": deep}
