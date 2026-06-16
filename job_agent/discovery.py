"""Weekly company discovery — PROPOSE-ONLY, never auto-added.

Pipeline: Claude (with web search) proposes Bay Area media/entertainment/consumer
companies that fit the candidate's full profile -> we EXCLUDE anything already on the
watchlist or already proposed/dismissed -> we INDEPENDENTLY VERIFY each candidate
(resolver probe of public ATS feeds, else an HTTP reachability check of a cited
careers URL). Only verified candidates become proposals (with a citable source URL);
everything else goes to an "unverified — not proposed" bucket. Nothing is auto-added
to companies.yaml; the user approves/dismisses.

Grounding: a company is never proposed on the model's word alone — it must resolve to
a real feed or have a reachable, cited careers page.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config, db, store
from .companies import Company, load_companies
from .reasoning import llm
from .sources import ats as ats_mod
from .sources import resolver as resolver_mod

DISCOVERY_SYSTEM = (
    "You help a Bay Area job seeker find NEW target companies. Use web search to find REAL "
    "companies (media, entertainment, gaming, consumer tech) headquartered in or hiring in the "
    "SF Bay Area whose roles would fit the candidate's background. For each, provide a real "
    "source URL (the company's careers page or a reputable listing) that you actually found. "
    "NEVER invent a company, domain, or URL. Return ONLY a JSON array."
)

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(name: str) -> str:
    return _NORM.sub(" ", (name or "").lower()).strip()


def _discovery_prompt(profile: Dict[str, Any], exclude: List[str], k: int) -> str:
    threads = ", ".join(profile.get("experience_threads") or profile.get("domains") or []) or "strategy, operations, business development"
    return f"""Find up to {k} NEW companies for this candidate to target.

Candidate threads: {threads}
Seniority: {profile.get('seniority')}  |  Summary: {profile.get('summary')}

Constraints:
- SF Bay Area (or strong Bay/US-remote presence), in media / entertainment / gaming / consumer tech.
- Roles should fit the candidate's FULL background above.
- Do NOT include any of these (already tracked or already seen): {', '.join(sorted(exclude)) or '(none)'}

Use web search to confirm each company is real and find its careers page.
Return ONLY a JSON array:
[{{"company": "Exact Name", "reason": "one line, grounded in why it fits the candidate", "evidence_url": "https://real-careers-or-listing-url"}}]"""


def _http_ok(url: Optional[str], session, timeout: int = 10) -> bool:
    """True if the URL is reachable (2xx/3xx). Verifies a cited careers page is real."""
    if not url or not re.match(r"^https?://", url):
        return False
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True,
                        headers={"User-Agent": "job-agent/0.1 (company discovery verifier)"})
        return 200 <= r.status_code < 400
    except Exception:
        return False


def should_run(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    if force:
        return True
    last = db.get_meta(conn, "last_discovery_at")
    if not last:
        return True
    try:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 86400
    except ValueError:
        return True
    return age_days >= config.DISCOVERY_INTERVAL_DAYS


def discover(
    conn: sqlite3.Connection, profile: Dict[str, Any], *,
    model: str = config.DEEP_MODEL, k: int = 12, session=None,
) -> Dict[str, List[dict]]:
    """Run one discovery scan. Returns {'proposed': [...], 'unverified': [...]}.
    Persists proposals/unverified to the suggestions table; stamps last_discovery_at."""
    import requests

    session = session or requests.Session()
    try:
        watch = {_norm(c.name) for c in load_companies()}
    except Exception:
        watch = set()
    exclude_norms = watch | store.existing_suggestion_names(conn)
    exclude_display = [c.name for c in (load_companies() if watch else [])]

    text, cited = llm.web_search(_discovery_prompt(profile, exclude_display, k),
                                 system=DISCOVERY_SYSTEM, model=model, max_tokens=4096)
    try:
        candidates = llm.parse_json(text)
    except llm.LLMError:
        candidates = []
    if not isinstance(candidates, list):
        candidates = []

    out: Dict[str, List[dict]] = {"proposed": [], "unverified": []}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = (c.get("company") or "").strip()
        norm = _norm(name)
        if not name or norm in exclude_norms:
            continue
        exclude_norms.add(norm)
        reason = (c.get("reason") or "").strip()
        evidence = (c.get("evidence_url") or "").strip()

        # 1) try to resolve a real public ATS feed (strongest verification)
        res = resolver_mod.resolve_company(Company(name=name, ats="auto"), session)
        if res.ok and res.slug:
            feed = ats_mod.board_url(res.ats, res.slug) or evidence
            if store.add_suggestion(conn, company=name, norm_name=norm, reason=reason,
                                    evidence_url=feed, ats=res.ats, slug=res.slug, status="proposed"):
                out["proposed"].append({"company": name, "reason": reason, "evidence_url": feed,
                                        "ats": res.ats, "slug": res.slug, "via": f"feed ({res.n_jobs} roles)"})
            continue

        # 2) else accept only if the cited careers URL is actually reachable
        if _http_ok(evidence, session):
            if store.add_suggestion(conn, company=name, norm_name=norm, reason=reason,
                                    evidence_url=evidence, ats=None, slug=None, status="proposed"):
                out["proposed"].append({"company": name, "reason": reason, "evidence_url": evidence,
                                        "ats": None, "slug": None, "via": "careers page"})
            continue

        # 3) unverifiable -> bucket, never propose
        store.add_suggestion(conn, company=name, norm_name=norm, reason=reason,
                             evidence_url=evidence or None, ats=None, slug=None, status="unverified")
        out["unverified"].append({"company": name, "reason": reason, "evidence_url": evidence})

    db.set_meta(conn, "last_discovery_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out


def approve(conn: sqlite3.Connection, sid: int, *, ats: Optional[str] = None, slug: Optional[str] = None) -> str:
    """Append an approved suggestion to companies.yaml (resolved or user-supplied
    ats+slug). Returns a status string. Never overwrites existing entries."""
    s = store.get_suggestion(conn, sid)
    if not s:
        return f"no suggestion with id {sid}"
    ats = ats or s["ats"]
    slug = slug or s["slug"]
    if not (ats and slug):
        return (f"'{s['company']}' has no resolved board. Re-run with "
                f"--ats <greenhouse|lever|ashby|workable|smartrecruiters|workday> --slug <board-slug>.")
    line = f'  - name: "{s["company"]}"\n    ats: {ats}\n    slug: {slug}\n'
    with open(config.COMPANIES_PATH, "a", encoding="utf-8") as fh:
        fh.write(line)
    store.set_suggestion_status(conn, sid, "approved")
    return f"approved '{s['company']}' -> companies.yaml ({ats}:{slug})"


def dismiss(conn: sqlite3.Connection, sid: int) -> str:
    s = store.get_suggestion(conn, sid)
    if not s:
        return f"no suggestion with id {sid}"
    store.set_suggestion_status(conn, sid, "dismissed")
    return f"dismissed '{s['company']}' (won't be proposed again)"
