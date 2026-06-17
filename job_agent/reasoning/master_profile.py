"""Synthesize ONE master profile (+ a voice profile) from all Drive materials.

The master profile is the UNION of the candidate's real experience across every
resume / CV / cover letter shared with the service account — not just the current
role. The voice profile captures tone/phrasing from the cover letters for drafting.

GROUNDING (critical): both may contain ONLY facts that appear in the documents.
Never invent an employer, title, date, degree, metric, or skill. Where documents
conflict, keep the most recent/most complete and record the discrepancy in
`variances`. Provenance (which files were used) is attached in code, not by the model.

Caching: keyed by a hash of the document set (id + modifiedTime); rebuilt only when
the set changes or --force.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .. import config, db, drive
from . import llm

MASTER_SYSTEM = (
    "You are a meticulous career analyst. You read MULTIPLE documents about ONE person "
    "(resumes, CVs, cover letters from different years) and synthesize a single, complete "
    "profile that is the UNION of everything true across them. You NEVER invent: every "
    "employer, title, date, degree, metric, and skill must appear in at least one document. "
    "When documents conflict (e.g. different titles or dates for the same role), keep the "
    "most recent / most complete version and record the discrepancy. Return ONLY one JSON object."
)

VOICE_SYSTEM = (
    "You analyze a person's COVER LETTERS to capture how they write — tone, characteristic "
    "phrasing, sentence rhythm, and how they frame themselves and their value. You describe "
    "the voice; you do not invent biographical facts. Return ONLY one JSON object."
)

_DOC_CHARS = 14000  # per-document cap fed to the model


def docset_hash(files: List[dict]) -> str:
    key = "|".join(f"{f.get('id')}:{f.get('modifiedTime')}" for f in sorted(files, key=lambda x: x.get("id", "")))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _docs_block(docs: List[Tuple[dict, str]]) -> str:
    out = []
    for i, (f, text) in enumerate(docs, 1):
        out.append(
            f"===== DOCUMENT {i}: {f.get('name')} "
            f"(modified {str(f.get('modifiedTime'))[:10]}) =====\n{text[:_DOC_CHARS]}"
        )
    return "\n\n".join(out)


def _master_prompt(docs: List[Tuple[dict, str]]) -> str:
    return f"""Synthesize ONE master profile from ALL of the documents below (they are about the SAME person).

Return ONLY a JSON object with exactly this shape:
{{
  "name": string|null,
  "contact": {{"email": string|null, "phone": string|null, "location": "City, ST"|null, "linkedin": string|null}},
  "seniority": one of ["IC","Manager","Director","VP","C-level"],
  "years_experience": number|null,
  "summary": string,                         // 3-4 sentence professional summary spanning the WHOLE career
  "experience_threads": [string],            // every major thread present, e.g. Strategy, Operations, Chief of Staff, Business Development, Dealmaking/Corp Dev, Campaign/Analytics, Leadership, Media — NOT only the latest role
  "domains": [string],
  "skills": [string],
  "industries": [string],
  "employers": [                             // full work history found across the docs
    {{"company": string, "titles": [string], "dates": string|null, "location": string|null, "highlights": [string]}}
  ],
  "achievements": [string],                  // quantified accomplishments (with the real numbers as written)
  "education": [string],
  "target_titles": [string],                 // roles this full background fits well
  "dealbreakers": [string],
  "nice_to_haves": [string],
  "variances": [string]                      // conflicts across docs you reconciled (title/date differences), or []
}}

Rules:
- Extract contact details (email, phone, current city/state, LinkedIn) from the documents for the resume header.
- UNION, not latest-only: include experience from EVERY document, even older roles.
- Use ONLY facts present in the documents. Do NOT invent or embellish anything.
- Keep real metrics exactly as written. If a field is unknown, use null or [].

DOCUMENTS:
{_docs_block(docs)}
"""


def _voice_prompt(cover_docs: List[Tuple[dict, str]]) -> str:
    return f"""From the COVER LETTER(S) below, capture how this person writes.

Return ONLY a JSON object with exactly this shape:
{{
  "tone": string,                       // e.g. "warm but precise; confident, not boastful"
  "characteristic_phrases": [string],   // real recurring phrasings/openings/closings (quote briefly)
  "self_framing": string,               // how they position themselves and their value
  "structure_notes": string,            // how their letters are organized (hook, evidence, close)
  "dos": [string],                      // stylistic do's to imitate
  "donts": [string]                     // things they avoid
}}

Rules:
- Describe the VOICE only; do not invent biographical facts.
- Base everything on the actual text provided.

COVER LETTERS:
{_docs_block(cover_docs)}
"""


def build_master_profile(docs: List[Tuple[dict, str]], *, model: str = config.DEEP_MODEL) -> Dict[str, Any]:
    profile = llm.complete_json(_master_prompt(docs), model=model, system=MASTER_SYSTEM, max_tokens=8192)
    if not isinstance(profile, dict):
        raise llm.LLMError("Master-profile synthesis did not return a JSON object.")
    return profile


def build_voice_profile(cover_docs: List[Tuple[dict, str]], *, model: str = config.DEEP_MODEL) -> Dict[str, Any]:
    if not cover_docs:
        return {"approximate": True, "note": "No cover letters found; voice is approximate.",
                "tone": None, "characteristic_phrases": [], "self_framing": None}
    voice = llm.complete_json(_voice_prompt(cover_docs), model=model, system=VOICE_SYSTEM, max_tokens=4096)
    if not isinstance(voice, dict):
        raise llm.LLMError("Voice-profile synthesis did not return a JSON object.")
    voice["approximate"] = len(cover_docs) < 2  # thin sample -> flag approximate
    return voice


def load_or_build(
    conn, *, force: bool = False, model: str = config.DEEP_MODEL
) -> Tuple[Dict[str, Any], Dict[str, Any], List[dict]]:
    """Return (master_profile, voice_profile, files_used). Builds from Drive when the
    document set changed (or --force); otherwise loads the cache.

    Raises drive.DriveError if no resume/cover-letter documents are shared with the SA.
    """
    files, docs = drive.collect()
    if not docs:
        sa = drive.service_account_email() or "the service account"
        try:
            visible = len(drive.list_all_visible(drive.build_service()))
        except Exception:
            visible = 0
        raise drive.DriveError(
            "No resumes / CVs / cover letters are shared with the service account "
            f"({sa}). It can currently see {visible} file(s). Please share your "
            "resume/cover-letter folder (Viewer) with that address, then re-run."
        )

    h = docset_hash(files)
    cached = db.get_meta(conn, "master_docset_hash")
    if (not force) and cached == h and config.MASTER_PROFILE_PATH.exists() and config.VOICE_PROFILE_PATH.exists():
        return (
            json.loads(config.MASTER_PROFILE_PATH.read_text(encoding="utf-8")),
            json.loads(config.VOICE_PROFILE_PATH.read_text(encoding="utf-8")),
            files,
        )

    master = build_master_profile(docs, model=model)
    cover_docs = [(f, t) for (f, t) in docs if drive.is_cover_letter(f)]
    voice = build_voice_profile(cover_docs, model=model)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    provenance = [
        {"name": f.get("name"), "id": f.get("id"), "type": f.get("mimeType"),
         "modified": f.get("modifiedTime"), "is_cover_letter": drive.is_cover_letter(f)}
        for f, _ in docs
    ]
    master["_meta"] = {"built_at": now, "model": model, "docset_hash": h, "source_documents": provenance}
    voice["_meta"] = {"built_at": now, "model": model,
                      "cover_letters_used": [f.get("name") for f, _ in cover_docs]}

    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    config.MASTER_PROFILE_PATH.write_text(json.dumps(master, indent=2, ensure_ascii=False), encoding="utf-8")
    config.VOICE_PROFILE_PATH.write_text(json.dumps(voice, indent=2, ensure_ascii=False), encoding="utf-8")
    db.set_meta(conn, "master_docset_hash", h)
    db.set_meta(conn, "master_profile_path", str(config.MASTER_PROFILE_PATH))
    return master, voice, files
