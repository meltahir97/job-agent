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

from . import config, drive, store
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
    "critical failure. NEVER mention business school, an MBA, or any graduate-school "
    "application, admission, or plan to attend — the candidate has no graduate degree and "
    "does not want this referenced. Return plain text using the exact ===SECTION=== markers "
    "requested — NOT JSON, no code fences."
)


def _draft_prompt(master: Dict[str, Any], voice: Dict[str, Any], job: sqlite3.Row) -> str:
    jd = (job["description"] or "")[:6000]
    facts = {k: v for k, v in master.items() if k != "_meta"}  # drop provenance (e.g. filenames)
    return f"""Produce a tailored resume and cover letter for this candidate and job.

MASTER PROFILE (the ONLY source of facts — do not go beyond it):
{json.dumps(facts, ensure_ascii=False, indent=2)[:16000]}

VOICE PROFILE (imitate this style in the cover letter):
{json.dumps(voice, ensure_ascii=False, indent=2)[:4000]}

JOB:
  Title: {job['title']}
  Company: {job['company']}
  Location: {job['location'] or 'n/a'}
  Description:
  \"\"\"{jd}\"\"\"

Return plain text in EXACTLY this structure, using these literal marker lines (no JSON, no code fences):

===RESUME===
<a complete, ATS-friendly resume in Markdown, built ONLY from the master profile, reordered/emphasized for THIS job: name + contact line + summary + experience (most relevant employers and bullets first) + skills + education>

===COVER_LETTER===
<a one-page cover letter in the candidate's voice, addressed to {job['company']}, connecting their REAL experience to this role; no invented facts>

===OMITTED===
<one bullet "- ..." per JD requirement the candidate does NOT clearly meet, so nothing was fabricated to cover it; or the single word: none>

Rules:
- Every employer, title, date, degree, metric, and skill MUST exist in the master profile.
- Prefer the candidate's real numbers exactly as written. Do not inflate.
- If unsure whether a fact is supported, leave it out."""


_SECTION_RE = re.compile(r"^===\s*(RESUME|COVER[_ ]?LETTER|OMITTED)\s*===\s*$", re.I | re.M)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n|\n```\s*$")


def _split_sections(text: str):
    """Parse the marker-delimited draft output into (resume_md, cover_md, omitted[])."""
    parts, matches = {}, list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        key = m.group(1).upper().replace(" ", "_")
        key = "COVER" if key.startswith("COVER") else key
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        body = _FENCE_RE.sub("", body).strip()  # tolerate stray code fences
        parts[key] = body
    omitted = [ln.strip(" -*\t") for ln in parts.get("OMITTED", "").splitlines()
               if ln.strip() and ln.strip().lower() not in ("none", "n/a", "- none")]
    return parts.get("RESUME", ""), parts.get("COVER", ""), omitted


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


def _build_docx(md: str):
    """Render a small Markdown subset (#/##/### headings, '-'/'*' bullets, **bold**,
    '---' rules, blank-line paragraphs) into a docx.Document."""
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
    return doc


def md_to_docx(md: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_docx(md).save(str(path))


def md_to_docx_bytes(md: str) -> bytes:
    import io
    buf = io.BytesIO()
    _build_docx(md).save(buf)
    return buf.getvalue()


_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str, n: int = 40) -> str:
    return _SLUG.sub("-", (s or "").lower()).strip("-")[:n] or "untitled"


def role_dir(company: str, title: str) -> Path:
    return config.APPLICATIONS_DIR / f"{_slug(company)}-{_slug(title)}"


def _save_local(conn, job, company, title, resume_md, cover_md, omitted, model) -> Dict[str, str]:
    d = role_dir(company, title)
    d.mkdir(parents=True, exist_ok=True)
    paths = {"resume_md": d / "resume.md", "resume_docx": d / "resume.docx",
             "cover_md": d / "cover_letter.md", "cover_docx": d / "cover_letter.docx"}
    resume_full = resume_md
    if omitted:  # honesty note as a markdown comment (kept out of the .docx body)
        resume_full += "\n\n<!-- JD requirements NOT claimed (no fabrication): " + "; ".join(map(str, omitted)) + " -->\n"
    paths["resume_md"].write_text(resume_full, encoding="utf-8")
    paths["cover_md"].write_text(cover_md, encoding="utf-8")
    md_to_docx(resume_md, paths["resume_docx"])
    md_to_docx(cover_md, paths["cover_docx"])
    store.record_draft(conn, job["id"], company=company, title=title, dir=str(d),
                       resume_md=paths["resume_md"], resume_docx=paths["resume_docx"],
                       cover_md=paths["cover_md"], cover_docx=paths["cover_docx"], model=model)
    return {"where": "local", "folder": str(d), "resume_url": str(paths["resume_docx"]),
            "cover_url": str(paths["cover_docx"])}


def generate_for_role(
    conn: sqlite3.Connection, job: sqlite3.Row, master: Dict[str, Any], voice: Dict[str, Any],
    *, model: str = config.DEEP_MODEL, regenerate: bool = False, to_drive: bool = True,
) -> Optional[Dict[str, str]]:
    """Generate + persist a draft set for one role. Writes editable Google Docs to the
    user's Drive (resume + cover letter), falling back to local files if Drive isn't
    available. Returns a dict of links, or None if already drafted (and not regenerate)."""
    if not regenerate and store.get_draft(conn, job["id"]):
        return None

    full = store.get_job(conn, job["id"]) or job  # select_master rows omit `description`
    text = llm.complete_text(_draft_prompt(master, voice, full), model=model, system=DRAFT_SYSTEM, max_tokens=12000)
    resume_md, cover_md, omitted = _split_sections(text)
    if not resume_md or not cover_md:
        raise llm.LLMError(f"Draft generation returned incomplete content for job {job['id']}.")
    company = job["company"] or "Company"
    title = job["title"] or "Role"

    if to_drive:
        try:
            svc = drive.build_service()
            parent, user_owned = drive.app_folder(conn, svc)
            sub = drive.ensure_subfolder(svc, parent, f"{company} — {title}"[:120])
            _, r_url = drive.upload_doc(svc, f"Resume — {company} — {title}"[:200], md_to_docx_bytes(resume_md), sub)
            _, c_url = drive.upload_doc(svc, f"Cover Letter — {company} — {title}"[:200], md_to_docx_bytes(cover_md), sub)
            folder_link = f"https://drive.google.com/drive/folders/{sub}"
            store.record_draft(conn, job["id"], company=company, title=title, dir=folder_link,
                               drive_url=folder_link, resume_url=r_url, cover_url=c_url, model=model)
            return {"where": "drive", "folder": folder_link, "resume_url": r_url,
                    "cover_url": c_url, "user_owned": user_owned}
        except drive.DriveError as e:
            print(f"   (Drive unavailable — {e}; saving locally instead.)")
        except Exception as e:  # never lose the generated draft over a Drive hiccup
            print(f"   (Drive upload failed — {e}; saving locally instead.)")

    return _save_local(conn, job, company, title, resume_md, cover_md, omitted, model)


def run_drafts(
    conn: sqlite3.Connection, rows: List[sqlite3.Row], master: Dict[str, Any], voice: Dict[str, Any],
    *, model: str = config.DEEP_MODEL, regenerate: bool = False, to_drive: bool = True,
) -> Tuple[int, int]:
    """Generate drafts for the given rows. Returns (generated, skipped)."""
    generated = skipped = 0
    for job in rows:
        try:
            res = generate_for_role(conn, job, master, voice, model=model, regenerate=regenerate, to_drive=to_drive)
        except llm.LLMError as e:
            print(f"   ! draft failed for [{job['id']}] {job['company']} – {job['title']}: {e}")
            continue
        if res is None:
            skipped += 1
        else:
            generated += 1
            where = res.get("folder") if res.get("where") == "drive" else role_dir(job["company"] or "", job["title"] or "").name + "/"
            print(f"   + drafted: {job['company']} – {job['title']} -> {where}")
    return generated, skipped
