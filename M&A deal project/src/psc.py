"""
psc.py
Resolve true ownership from the Companies House PSC register + officer ages.

The PSC (Persons with Significant Control) register is the deal-origination
signal that matters: it tells you who actually controls a private company.
  - Top PSC is an INDIVIDUAL with 75-100% control  -> founder-owned. TARGET.
  - Top PSC is a CORPORATE ENTITY                   -> part of a group. DROP.

Run:  python psc.py <company_number>
"""
from __future__ import annotations

import sys
import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from .companies_house import _get  # reuse the throttled client


# CH "natures of control" bands -> representative ownership %.
_CONTROL_BANDS = {
    "ownership-of-shares-75-to-100-percent": 87,
    "ownership-of-shares-50-to-75-percent": 62,
    "ownership-of-shares-25-to-50-percent": 37,
    "voting-rights-75-to-100-percent": 87,
    "voting-rights-50-to-75-percent": 62,
    "voting-rights-25-to-50-percent": 37,
}


@dataclass
class Owner:
    name: Optional[str]
    kind: str                       # individual | corporate | legal-person | unknown | none
    control_pct: Optional[int]
    age: Optional[int]
    nationality: Optional[str] = None
    role: str = "Person with Significant Control"

    @property
    def is_founder_owned(self) -> bool:
        return self.kind == "individual" and (self.control_pct or 0) >= 25


def _band_to_pct(natures: list[str]) -> Optional[int]:
    pcts = [_CONTROL_BANDS[n] for n in natures if n in _CONTROL_BANDS]
    return max(pcts) if pcts else None


def _age_from_dob(dob: dict | None) -> Optional[int]:
    # CH exposes only month + year of birth, so age is year-accurate.
    if not dob or "year" not in dob:
        return None
    return _dt.date.today().year - int(dob["year"])


def get_psc(company_number: str) -> list[dict]:
    data = _get(f"/company/{company_number}/persons-with-significant-control")
    return data.get("items", []) if data else []


def get_officers(company_number: str) -> list[dict]:
    data = _get(f"/company/{company_number}/officers")
    return data.get("items", []) if data else []


def resolve_owner(company_number: str) -> Owner:
    """Return the controlling owner. kind='corporate' => disqualify (group-owned)."""
    pscs = get_psc(company_number)
    if not pscs:
        return Owner(name=None, kind="none", control_pct=None, age=None)

    def control_of(p: dict) -> int:
        return _band_to_pct(p.get("natures_of_control", [])) or 0

    # Highest control wins; ties favour individuals.
    pscs_sorted = sorted(
        pscs,
        key=lambda p: (control_of(p), "individual" in p.get("kind", "")),
        reverse=True,
    )
    top = pscs_sorted[0]
    kind_raw = top.get("kind", "")

    if "individual" in kind_raw:
        kind = "individual"
    elif "corporate" in kind_raw:
        kind = "corporate"
    elif "legal-person" in kind_raw:
        kind = "legal-person"
    else:
        kind = "unknown"

    age = _age_from_dob(top.get("date_of_birth")) if kind == "individual" else None

    # Backfill age from the officers list if the PSC record omits DoB (surname match — heuristic).
    if kind == "individual" and age is None and top.get("name"):
        surname = top["name"].lower().split()[-1]
        for off in get_officers(company_number):
            if surname in (off.get("name") or "").lower():
                age = _age_from_dob(off.get("date_of_birth"))
                if age:
                    break

    return Owner(
        name=top.get("name"),
        kind=kind,
        control_pct=control_of(top) or None,
        age=age,
        nationality=top.get("nationality"),
    )


def succession_signal(
    owner: Owner,
    years_in_business: Optional[int],
    *,
    min_age: int = 55,
    min_years: int = 10,
) -> bool:
    """True when owner age + business age suggest a plausible exit window."""
    if owner.age is None or years_in_business is None:
        return False
    return owner.age >= min_age and years_in_business >= min_years


if __name__ == "__main__":
    cn = sys.argv[1] if len(sys.argv) > 1 else "00041424"
    owner = resolve_owner(cn)
    print(owner)
    print("founder-owned:", owner.is_founder_owned)
