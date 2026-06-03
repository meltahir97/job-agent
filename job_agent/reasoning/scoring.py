"""Two-stage scoring: cheap triage (haiku) then deep scoring (sonnet/opus).

Grounding is enforced structurally:
  * The model only ever sees the job fields we pass it (as JSON data).
  * Results are matched back by the job's exact `id`; any id the model invents
    that wasn't in the batch is ignored. Missing salaries/fields stay null.
Cost control: only un-triaged jobs are triaged, only triage survivors are deep-
scored, and both stages are batched into few calls.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .. import config, store
from . import llm

TRIAGE_SYSTEM = (
    "You are a fast, decisive job-fit triager. Given a candidate profile and a list of job "
    "listings provided as data, decide for EACH listing whether it is plausibly worth a deeper "
    "look for THIS candidate. Be permissive: drop only obvious non-matches (clearly wrong "
    "seniority, wrong function, irrelevant domain, or a location incompatible with Bay Area / "
    "Remote). Use ONLY the provided fields; never invent details. Return ONLY a JSON array."
)

DEEP_SYSTEM = (
    "You are a rigorous job-fit evaluator for one specific candidate. For EACH provided listing, "
    "produce a 0-100 fit score, a 2-3 sentence rationale that cites SPECIFIC overlap between the "
    "candidate's profile and the job's description, any red flags evident in the listing, and a "
    "label. Use ONLY the data provided; never invent salary, requirements, or any fact not present "
    "in the listing. Return ONLY a JSON array."
)

PROFILE_KEYS = [
    "name", "seniority", "years_experience", "domains", "skills",
    "industries", "target_titles", "dealbreakers", "nice_to_haves", "summary",
]


def _profile_brief(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {k: profile.get(k) for k in PROFILE_KEYS if k in profile}


def _job_payload(row: sqlite3.Row, *, desc_chars: int) -> Dict[str, Any]:
    salary = None
    if row["salary_min"] or row["salary_max"]:
        salary = {
            "min": row["salary_min"],
            "max": row["salary_max"],
            "currency": row["salary_currency"],
        }
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


def _chunks(seq: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _index_results(result: Any) -> Dict[int, dict]:
    """Map model output back to job ids, tolerating minor shape variations."""
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
        return "skip"
    if score >= 75:
        return "match"
    if score >= 50:
        return "stretch"
    return "skip"


def _triage_prompt(profile: Dict[str, Any], jobs: List[dict]) -> str:
    return (
        "CANDIDATE PROFILE:\n"
        f"{json.dumps(_profile_brief(profile), indent=2, ensure_ascii=False)}\n\n"
        "JOB LISTINGS (data — score only these, by their exact id):\n"
        f"{json.dumps(jobs, indent=2, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON array, one object per listing:\n"
        '[{"id": <int, exactly as given>, "keep": <true|false>, "reason": "<one short phrase>"}]\n'
        "Use only the provided fields. Do not add listings or invent ids."
    )


def _deep_prompt(profile: Dict[str, Any], jobs: List[dict], feedback: Optional[List[sqlite3.Row]]) -> str:
    fb_block = ""
    if feedback:
        saved = [f"{r['title']} @ {r['company']}" for r in feedback if r["decision"] == "saved"]
        dismissed = [f"{r['title']} @ {r['company']}" for r in feedback if r["decision"] == "dismissed"]
        if saved or dismissed:
            fb_block = (
                "\nCANDIDATE FEEDBACK (weight these preferences):\n"
                f"  SAVED (liked): {saved or 'none'}\n"
                f"  DISMISSED (not interested): {dismissed or 'none'}\n"
            )
    return (
        "CANDIDATE PROFILE:\n"
        f"{json.dumps(_profile_brief(profile), indent=2, ensure_ascii=False)}\n"
        f"{fb_block}\n"
        "JOB LISTINGS (data — evaluate only these, by their exact id):\n"
        f"{json.dumps(jobs, indent=2, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON array, one object per listing:\n"
        "[{\n"
        '  "id": <int, exactly as given>,\n'
        '  "fit_score": <int 0-100>,\n'
        '  "label": "match" | "stretch" | "skip",\n'
        '  "rationale": "<2-3 sentences citing specific profile/JD overlap>",\n'
        '  "red_flags": ["<any concerns evident in the listing>"]\n'
        "}]\n"
        "Cite only facts present in the listing or profile. If the JD lacks detail, say so rather "
        "than assuming. Do not invent salary or requirements."
    )


def triage(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    rows: List[sqlite3.Row],
    *,
    model: str = config.TRIAGE_MODEL,
    batch_size: int = 25,
) -> int:
    kept = 0
    for batch in _chunks(rows, batch_size):
        payload = [_job_payload(r, desc_chars=600) for r in batch]
        result = llm.complete_json(_triage_prompt(profile, payload), model=model, system=TRIAGE_SYSTEM)
        decided = _index_results(result)
        for r in batch:
            d = decided.get(r["id"]) or {}
            keep = bool(d.get("keep"))
            store.record_score(conn, r["id"], stage="triage", model=model, keep=keep, rationale=d.get("reason"))
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
    n = 0
    for batch in _chunks(rows, batch_size):
        payload = [_job_payload(r, desc_chars=2000) for r in batch]
        result = llm.complete_json(_deep_prompt(profile, payload, feedback), model=model, system=DEEP_SYSTEM)
        decided = _index_results(result)
        for r in batch:
            d = decided.get(r["id"])
            if not d:  # model omitted this id — record an explicit null, never fabricate
                store.record_score(
                    conn, r["id"], stage="deep", model=model,
                    fit_score=None, label="skip", rationale="No score returned for this listing.",
                )
                continue
            score = _clamp_score(d.get("fit_score"))
            label = d.get("label") if d.get("label") in ("match", "stretch", "skip") else _label_from_score(score)
            red = d.get("red_flags")
            store.record_score(
                conn, r["id"], stage="deep", model=model,
                fit_score=score, label=label, rationale=d.get("rationale"),
                red_flags=red if isinstance(red, list) else None,
            )
            n += 1
    return n


def run_scoring(conn: sqlite3.Connection, profile: Dict[str, Any], *, deep_model: str = config.DEEP_MODEL) -> Dict[str, int]:
    to_triage = store.jobs_needing_triage(conn)
    kept = triage(conn, profile, to_triage) if to_triage else 0
    to_deep = store.jobs_needing_deep(conn)
    feedback = store.feedback_examples(conn)
    deep = deep_score(conn, profile, to_deep, model=deep_model, feedback=feedback) if to_deep else 0
    return {"triaged": len(to_triage), "kept": kept, "deep_scored": deep}
