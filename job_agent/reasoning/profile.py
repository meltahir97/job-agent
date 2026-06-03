"""Resume -> structured profile, parsed once and cached.

The resume PDF is text-extracted locally (pypdf, no LLM), sent to Claude once to
produce a structured JSON profile, and cached to profile/profile.json. We store a
SHA-256 of the resume file in `meta`; the profile is only re-built when that hash
changes (or --force), so we never pay to re-parse an unchanged resume.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

from .. import config, db
from . import llm

PROFILE_SYSTEM = (
    "You are a precise resume parser. You extract structured facts from a resume and "
    "return ONLY a single JSON object. You never invent information that is not present "
    "in the resume; when a field is unknown, use null or an empty list. You use no tools."
)


def extract_resume_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prompt(resume_text: str) -> str:
    return f"""Extract a structured candidate profile from the resume text below.

Return ONLY a JSON object with exactly this shape:
{{
  "name": string|null,
  "current_title": string|null,
  "seniority": one of ["IC","Manager","Director","VP","C-level"],
  "years_experience": number|null,
  "domains": [string],          // functional areas, e.g. Strategy, Operations, Business Development, Corporate Development
  "skills": [string],
  "industries": [string],
  "education": [string],
  "target_titles": [string],    // role titles this candidate is a strong fit for
  "dealbreakers": [string],     // infer conservatively; [] if none are evident
  "nice_to_haves": [string],
  "summary": string             // 2-3 sentence professional summary
}}

Rules:
- Use ONLY information found in the resume. Do NOT invent employers, titles, dates, degrees, or skills.
- If a field cannot be determined, use null (scalars) or [] (lists).
- "seniority" is your best single estimate of the candidate's current level.

RESUME:
\"\"\"
{resume_text}
\"\"\"
"""


def build_profile(resume_text: str, *, model: str = config.DEEP_MODEL) -> Dict[str, Any]:
    profile = llm.complete_json(_prompt(resume_text), model=model, system=PROFILE_SYSTEM)
    if not isinstance(profile, dict):
        raise llm.LLMError("Profile extraction did not return a JSON object.")
    return profile


def load_or_build(conn: sqlite3.Connection, *, force: bool = False, model: str = config.DEEP_MODEL) -> Dict[str, Any]:
    path = config.RESUME_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Resume not found at {path}. Set RESUME_PATH in .env or place it at "
            f"{config.PROFILE_DIR.parent / 'resume' / 'resume.pdf'}."
        )

    current_hash = file_hash(path)
    cached_hash = db.get_meta(conn, "resume_hash")
    if (not force) and config.PROFILE_PATH.exists() and cached_hash == current_hash:
        return json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))

    text = extract_resume_text(path)
    if len(text) < 50:
        raise llm.LLMError(
            f"Extracted only {len(text)} chars from {path.name}; the PDF may be scanned/image-only."
        )

    profile = build_profile(text, model=model)
    profile["_meta"] = {
        "resume_file": str(path),
        "resume_hash": current_hash,
        "model": model,
        "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    config.PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    db.set_meta(conn, "resume_hash", current_hash)
    db.set_meta(conn, "profile_path", str(config.PROFILE_PATH))
    return profile
