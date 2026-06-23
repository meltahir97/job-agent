"""Watchlist loader: read target companies from companies.yaml.

Schema (top-level `companies:` list):
    - name: Acme Corp           # display name (required)
      ats: greenhouse           # greenhouse | lever | ashby | workable | auto
      slug: acme                # board token; required unless ats == auto

`ats: auto` lets the resolver discover the board from public ATS URL patterns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import config

VALID_ATS = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday",
             "google", "netflix", "apple", "auto"}
# ATS types that resolve themselves without a board slug (custom/query-based sources).
_NO_SLUG = {"auto", "google", "netflix", "apple"}


@dataclass
class Company:
    name: str
    ats: str = "auto"
    slug: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)  # source-specific (e.g. Workday dc/site)


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
        if ats not in _NO_SLUG and not slug:
            raise CompaniesError(f"{name}: 'slug' is required when ats={ats}.")
        # preserve list-valued extras (e.g. Google `queries`); stringify scalars (Workday dc/site)
        extra = {k: (v if isinstance(v, list) else str(v))
                 for k, v in e.items() if k not in ("name", "ats", "slug") and v is not None}
        if ats == "workday" and not (extra.get("dc") and extra.get("site")):
            raise CompaniesError(f"{name}: ats=workday needs 'dc' (e.g. wd5) and 'site' fields.")
        out.append(Company(name=name, ats=ats, slug=slug, extra=extra))
    return out
