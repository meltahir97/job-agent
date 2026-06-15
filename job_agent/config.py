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

# --- Watchlist location filter (TUNE HERE) ---------------------------------
# KEEP a job if it is in the Bay Area OR remote-inclusive of the US/California;
# DROP only if clearly elsewhere-only; if ambiguous, KEEP with remote=None.
# *_TERMS match as substrings; *_TOKENS match as whole words so short abbreviations
# (us / ca / uk) don't accidentally match inside other words (e.g. "Austin").
BAY_AREA_TERMS = [
    "san francisco", "sf bay", "bay area", "silicon valley", "oakland", "san jose",
    "berkeley", "palo alto", "menlo park", "mountain view", "sunnyvale", "santa clara",
    "redwood city", "south san francisco", "san mateo", "foster city", "cupertino",
    "emeryville", "burlingame", "fremont", "alameda", "peninsula", "walnut creek",
    "san carlos", "san bruno", "daly city", "milpitas", "los gatos", "campbell", "hayward",
]
BAY_AREA_TOKENS = {"sf"}

REMOTE_TERMS = ["remote", "work from home", "wfh", "distributed", "anywhere", "telecommute"]

# Signals that a (remote) role includes the US/California.
US_TERMS = ["united states", "u.s.", "usa", "america", "north america", "california", "nationwide"]
US_TOKENS = {"us", "usa", "ca"}

# Clearly-not-US places (drop remote-non-US and onsite-elsewhere).
NON_US_TERMS = [
    "united kingdom", "london", "england", "scotland", "ireland", "dublin", "germany",
    "berlin", "munich", "france", "paris", "spain", "madrid", "barcelona", "netherlands",
    "amsterdam", "canada", "toronto", "vancouver", "ontario", "india", "bangalore",
    "bengaluru", "hyderabad", "pune", "gurgaon", "singapore", "australia", "sydney",
    "melbourne", "israel", "tel aviv", "brazil", "sao paulo", "mexico", "poland", "warsaw",
    "krakow", "portugal", "lisbon", "japan", "tokyo", "china", "shanghai", "hong kong",
    "united arab emirates", "dubai", "europe", "emea", "apac", "latam",
    # additions (observed leaks + common hubs)
    "south korea", "korea", "seoul", "mumbai", "delhi", "taiwan", "taipei", "colombia",
    "bogota", "bogotá", "philippines", "manila", "indonesia", "jakarta", "thailand",
    "bangkok", "vietnam", "malaysia", "kuala lumpur", "sweden", "stockholm", "switzerland",
    "zurich", "norway", "oslo", "denmark", "copenhagen", "finland", "helsinki", "austria",
    "vienna", "belgium", "brussels", "italy", "milan", "rome", "greece", "athens", "turkey",
    "istanbul", "egypt", "cairo", "nigeria", "lagos", "kenya", "nairobi", "new zealand",
    "auckland", "argentina", "buenos aires", "chile", "santiago", "czech", "prague",
    "hungary", "budapest", "romania", "bucharest", "remote - emea", "remote - apac",
]
NON_US_TOKENS = {"uk", "eu", "gb"}

# Major US hubs outside the Bay Area (drop onsite roles clearly anchored there).
US_NON_BAY_TERMS = [
    "new york", "nyc", "brooklyn", "seattle", "austin", "boston", "chicago",
    "los angeles", "san diego", "denver", "atlanta", "washington, dc", "washington dc",
    "dallas", "houston", "miami", "philadelphia", "phoenix", "portland", "nashville",
    "minneapolis", "detroit", "salt lake city", "raleigh", "pittsburgh", "columbus", "irvine",
    "santa monica", "stamford", "cary", "san antonio", "kansas city", "charlotte", "tampa",
]


def ensure_dirs() -> None:
    for d in (DATA_DIR, DIGEST_DIR, PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
