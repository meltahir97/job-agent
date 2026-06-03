"""Configuration: paths, secrets, model names, and search defaults.

Secrets come from the environment / a local .env file (never hardcoded).
A tiny dependency-free .env loader is used so the pure-Python data layer has
no third-party requirements.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE per line, '#' comments, real env wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and not os.environ.get(key):  # fill if unset OR blank; real values win
            os.environ[key] = val


_load_dotenv(BASE_DIR / ".env")

# --- Paths ---
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "jobs.db"
DIGEST_DIR = BASE_DIR / "digests"
PROFILE_DIR = BASE_DIR / "profile"
PROFILE_PATH = PROFILE_DIR / "profile.json"
RESUME_PATH = Path(os.environ.get("RESUME_PATH") or (BASE_DIR / "resume" / "resume.pdf"))
COMPANIES_PATH = Path(os.environ.get("COMPANIES_PATH") or (BASE_DIR / "companies.yaml"))

# --- Secrets (from .env / environment) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")

# --- Models (exact IDs requested) ---
TRIAGE_MODEL = "claude-haiku-4-5"   # cheap keep/drop triage
DEEP_MODEL = "claude-sonnet-4-6"    # default deep scoring
STRONG_MODEL = "claude-opus-4-8"    # opt-in stronger deep scoring

# --- Search defaults (your parameters) ---
# Manager / Director / VP-level Strategy, Operations, BizDev, Corp Dev — Bay Area or Remote.
TARGET_TITLES = ["Manager", "Director", "VP", "Vice President", "Head of"]
TARGET_DOMAINS = [
    "Strategy",
    "Operations",
    "Business Development",
    "Corporate Development",
]
LOCATIONS = ["San Francisco Bay Area", "Remote"]
COUNTRY = "us"  # Adzuna country code


def ensure_dirs() -> None:
    for d in (DATA_DIR, DIGEST_DIR, PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
