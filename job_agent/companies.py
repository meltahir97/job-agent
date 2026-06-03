"""Watchlist loader: read target companies from companies.yaml.

Schema (top-level `companies:` list):
    - name: Acme Corp           # display name (required)
      ats: greenhouse           # greenhouse | lever | ashby | workable | auto
      slug: acme                # board token; required unless ats == auto

`ats: auto` lets the resolver discover the board from public ATS URL patterns.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config

VALID_ATS = {"greenhouse", "lever", "ashby", "workable", "auto"}


@dataclass
class Company:
    name: str
    ats: str = "auto"
    slug: Optional[str] = None


class CompaniesError(RuntimeError):
    pass


def load_companies(path: Optional[Path] = None) -> List[Company]:
    import yaml

    path = Path(path) if path else config.COMPANIES_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found at {path}. Create it (see companies.yaml example)."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("companies") if isinstance(data, dict) else data
    if not entries:
        raise CompaniesError(f"{path.name} has no companies listed.")

    out: List[Company] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise CompaniesError(f"{path.name} entry #{i + 1} is not a mapping.")
        name = (e.get("name") or "").strip()
        ats = (e.get("ats") or "auto").strip().lower()
        slug = e.get("slug")
        slug = slug.strip() if isinstance(slug, str) else None
        if not name:
            raise CompaniesError(f"{path.name} entry #{i + 1} is missing 'name'.")
        if ats not in VALID_ATS:
            raise CompaniesError(
                f"{name}: invalid ats '{ats}' (use one of {sorted(VALID_ATS)})."
            )
        if ats != "auto" and not slug:
            raise CompaniesError(f"{name}: 'slug' is required when ats={ats}.")
        out.append(Company(name=name, ats=ats, slug=slug))
    return out
