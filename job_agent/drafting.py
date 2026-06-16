"""Generate tailored resume + cover-letter drafts for surfaced roles.

Sole source of facts = the cached MASTER PROFILE; tone = the VOICE PROFILE. Output
is editable .md AND .docx under ./applications/<company>-<role>/. Drafts are LOCAL
only (never published) and idempotent (skip already-drafted unless regenerate).

ABSOLUTE GROUNDING: tailoring is selection / emphasis / ordering / rewording of TRUE
content only. The model is instructed never to invent or embellish an employer,
title, date, degree, metric, or skill. If the JD wants something the candidate lacks,
the draft must not claim it (the cover letter may honestly frame transferable work).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, store
from .reasoning import llm

def load_profiles() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Load the cached master + voice profiles for drafting. Master is required."""
    if not config.MASTER_PROFILE_PATH.exists():
        raise FileNotFoundError("Master profile not built yet — run `job-agent master-profile` first.")
    master = json.loads(config.MASTER_PROFILE_PATH.read_text(encoding="utf-8"))
    voice = (json.loads(config.VOICE_PROFILE_PATH.read_text(encoding="utf-8"))
             if config.VOICE_PROFILE_PATH.exists() else {"approximate": True})
    return master, voice


DRAFT_SYSTEM = (
    "You tailor a candidate's REAL experience to a specific job posting. ABSOLUTE GROUNDING: "
    "you may use ONLY facts that appear in the MASTER PROFILE provided — never invent or "
    "embellish an employer, title, date, degree, metric, certification, or skill. Tailoring "
    "means selecting, emphasizing, ordering, and rewording TRUE content to fit the job; it "
    "NEVER means adding new claims. If the job wants something the candidate does not have, do "
    "NOT claim it — instead the cover letter may honestly frame transferable experience. Write "
    "the cover letter in the candidate's own VOICE (provided). A fabricated credential is a "
    "critical failure. Return ONLY a JSON object."
)


def _draft_prompt(master: Dict[str, Any], voice: Dict[str, Any], job: sqlite3.Row) -> str:
    jd = (job["description"] or "")[:6000]
    return f"""Produce a tailored resume and cover letter for this candidate and job.

MASTER PROFILE (the ONLY source of facts — do not go beyond it):
{json.dumps(master, ensure_ascii=False, indent=2)[:16000]}

VOICE PROFILE (imitate this style in the cover letter):
{json.dumps(voice, ensure_ascii=False, indent=2)[:4000]}

JOB:
  Title: {job['title']}
  Company: {job['company']}
  Location: {job['location'] or 'n/a'}
  Description:
  \"\"\"{jd}\"\"\"

Return ONLY a JSON object:
{{
  "resume_markdown": "a complete, ATS-friendly resume in Markdown, built ONLY from the master profile, reordered/emphasized for THIS job (name + summary + experience with the most relevant employers and bullets first + skills + education)",
  "cover_letter_markdown": "a one-page cover letter in the candidate's voice, addressed to {job['company']}, connecting their REAL experience to this role; no invented facts",
  "omitted_requirements": ["JD requirements the candidate does NOT clearly meet (so nothing was fabricated to cover them)"]
}}

Rules:
- Every employer, title, date, degree, metric, and skill MUST exist in the master profile.
- Prefer the candidate's real numbers exactly as written. Do not inflate.
- If unsure whether a fact is supported, leave it out."""


# --- markdown -> docx (minimal, dependency-light) ---------------------------

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_runs(paragraph, text: str) -> None:
    """Render inline **bold** within a docx paragraph; everything else plain."""
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        paragraph.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def md_to_docx(md: str, path: Path) -> None:
    """Render a small Markdown subset (#/##/### headings, '-'/'*' bullets, **bold**,
    '---' rules, blank-line paragraphs) into an editable .docx."""
    import docx

    doc = docx.Document()
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.strip() in ("---", "***", "___"):
            doc.add_paragraph().add_run("—" * 20)
        elif line.lstrip().startswith(("- ", "* ")):
            _add_runs(doc.add_paragraph(style="List Bullet"), line.lstrip()[2:])
        else:
            _add_runs(doc.add_paragraph(), line)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str, n: int = 40) -> str:
    return _SLUG.sub("-", (s or "").lower()).strip("-")[:n] or "untitled"


def role_dir(company: str, title: str) -> Path:
    return config.APPLICATIONS_DIR / f"{_slug(company)}-{_slug(title)}"


def generate_for_role(
    conn: sqlite3.Connection, job: sqlite3.Row, master: Dict[str, Any], voice: Dict[str, Any],
    *, model: str = config.DEEP_MODEL, regenerate: bool = False,
) -> Optional[Dict[str, str]]:
    """Generate + persist a draft set for one role. Returns the file paths, or None
    if a draft already exists and regenerate is False (idempotent)."""
    if not regenerate and store.get_draft(conn, job["id"]):
        return None

    result = llm.complete_json(_draft_prompt(master, voice, job), model=model, system=DRAFT_SYSTEM, max_tokens=8192)
    resume_md = (result.get("resume_markdown") or "").strip()
    cover_md = (result.get("cover_letter_markdown") or "").strip()
    if not resume_md or not cover_md:
        raise llm.LLMError(f"Draft generation returned empty content for job {job['id']}.")

    d = role_dir(job["company"] or "company", job["title"] or "role")
    d.mkdir(parents=True, exist_ok=True)
    paths = {
        "resume_md": d / "resume.md",
        "resume_docx": d / "resume.docx",
        "cover_md": d / "cover_letter.md",
        "cover_docx": d / "cover_letter.docx",
    }
    omitted = result.get("omitted_requirements") or []
    resume_full = resume_md
    cover_full = cover_md
    if omitted:  # keep an honesty note in the markdown (not in the docx body)
        note = "\n\n<!-- JD requirements NOT claimed (no fabrication): " + "; ".join(map(str, omitted)) + " -->\n"
        resume_full += note

    paths["resume_md"].write_text(resume_full, encoding="utf-8")
    paths["cover_md"].write_text(cover_full, encoding="utf-8")
    md_to_docx(resume_md, paths["resume_docx"])
    md_to_docx(cover_md, paths["cover_docx"])

    store.record_draft(
        conn, job["id"], company=job["company"], title=job["title"], dir=d,
        resume_md=paths["resume_md"], resume_docx=paths["resume_docx"],
        cover_md=paths["cover_md"], cover_docx=paths["cover_docx"], model=model,
    )
    return {k: str(v) for k, v in paths.items()}


def run_drafts(
    conn: sqlite3.Connection, rows: List[sqlite3.Row], master: Dict[str, Any], voice: Dict[str, Any],
    *, model: str = config.DEEP_MODEL, regenerate: bool = False,
) -> Tuple[int, int]:
    """Generate drafts for the given rows. Returns (generated, skipped)."""
    generated = skipped = 0
    for job in rows:
        try:
            res = generate_for_role(conn, job, master, voice, model=model, regenerate=regenerate)
        except llm.LLMError as e:
            print(f"   ! draft failed for [{job['id']}] {job['company']} – {job['title']}: {e}")
            continue
        if res is None:
            skipped += 1
        else:
            generated += 1
            print(f"   + drafted: {job['company']} – {job['title']} -> {role_dir(job['company'] or '', job['title'] or '').name}/")
    return generated, skipped
