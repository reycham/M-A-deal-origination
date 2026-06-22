"""
fit_score.py
Apply thesis.yaml weights to produce a 0–100 fit score + A/B/C tier.

Score components (matching the spec):
  industry_match   20  — LLM confirms genuine MSP
  size_band        20  — headcount in thesis employee_band
  founder_owned    20  — PSC individual + no funding rounds
  succession       20  — owner age ≥ min + years_in_business ≥ min
  standalone       10  — PSC not corporate (no group structure)
  recurring_rev    10  — LLM recurring-rev confidence (0–100 → scaled to 0–10)

Hard disqualifiers → score = 0 immediately.

Deps: pip install pyyaml
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .companies_house import Company
from .psc import Owner

logger = logging.getLogger(__name__)

_DEFAULT_THESIS = Path(__file__).parent.parent / "config" / "thesis.yaml"


def _load_thesis(path: Path = _DEFAULT_THESIS) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class ScoreResult:
    score: int
    tier: str                          # "A" | "B" | "C"
    breakdown: dict[str, int]
    disqualified_by: list[str] = field(default_factory=list)

    @property
    def is_qualified(self) -> bool:
        return not self.disqualified_by


def _tier(score: int, tiers: dict) -> str:
    if score >= tiers.get("A", 80):
        return "A"
    if score >= tiers.get("B", 60):
        return "B"
    return "C"


def score_company(
    company: Company,
    owner: Owner,
    *,
    employee_count: Optional[int] = None,
    has_funding: bool = False,
    msp_label: str = "unclear",        # "msp" | "adjacent" | "unclear"
    msp_confidence: int = 0,           # 0–100
    recurring_rev_confidence: int = 0, # 0–100
    thesis_path: Path = _DEFAULT_THESIS,
) -> ScoreResult:
    thesis = _load_thesis(thesis_path)
    weights = thesis.get("weights", {})
    tiers = thesis.get("tiers", {"A": 80, "B": 60})
    emp_band = thesis.get("employee_band", {"min": 10, "max": 50})
    succession_cfg = thesis.get("succession", {"min_owner_age": 55})
    min_years = thesis.get("min_years_in_business", 10)

    disqualifiers: list[str] = []

    # ── Hard disqualifiers ────────────────────────────────────────────────────
    if owner.kind == "corporate":
        disqualifiers.append("psc_is_company")
    if has_funding:
        disqualifiers.append("has_funding")
    if employee_count is not None and employee_count < 5:
        disqualifiers.append("under_5_employees")
    if company.company_status.lower() not in ("active", ""):
        disqualifiers.append("not_active")

    if disqualifiers:
        return ScoreResult(score=0, tier="C", breakdown={}, disqualified_by=disqualifiers)

    breakdown: dict[str, int] = {}

    # ── 1. Industry match (20) ────────────────────────────────────────────────
    w = weights.get("industry_match", 20)
    if msp_label == "msp":
        pts = w
    elif msp_label == "unclear":
        pts = round(w * 0.5)
    else:
        pts = 0
    breakdown["industry_match"] = pts

    # ── 2. Size band (20) ─────────────────────────────────────────────────────
    w = weights.get("size_band", 20)
    if employee_count is None:
        pts = round(w * 0.4)           # partial credit — we just don't know
    elif emp_band["min"] <= employee_count <= emp_band["max"]:
        pts = w
    elif emp_band["min"] // 2 <= employee_count < emp_band["min"] or \
         emp_band["max"] < employee_count <= emp_band["max"] * 1.5:
        pts = round(w * 0.5)           # adjacent band
    else:
        pts = 0
    breakdown["size_band"] = pts

    # ── 3. Founder-owned (20) ─────────────────────────────────────────────────
    w = weights.get("founder_owned", 20)
    if owner.is_founder_owned and not has_funding:
        pts = w
    elif owner.is_founder_owned:
        pts = round(w * 0.5)
    else:
        pts = 0
    breakdown["founder_owned"] = pts

    # ── 4. Succession signal (20) ─────────────────────────────────────────────
    w = weights.get("succession", 20)
    min_age = succession_cfg.get("min_owner_age", 55)
    age_ok = (owner.age or 0) >= min_age
    years_ok = (company.years_in_business or 0) >= min_years
    if age_ok and years_ok:
        pts = w
    elif age_ok or years_ok:
        pts = round(w * 0.5)
    else:
        pts = 0
    breakdown["succession"] = pts

    # ── 5. Standalone / not a subsidiary (10) ────────────────────────────────
    w = weights.get("standalone", 10)
    pts = w if owner.kind == "individual" else round(w * 0.3)
    breakdown["standalone"] = pts

    # ── 6. Recurring revenue (10) ────────────────────────────────────────────
    w = weights.get("recurring_rev", 10)
    # Normalise: LLMs sometimes return 0-1 instead of 0-100
    rec_conf = recurring_rev_confidence
    if rec_conf <= 1.0:
        rec_conf = rec_conf * 100
    pts = round(rec_conf / 100 * w)
    breakdown["recurring_rev"] = pts

    total = sum(breakdown.values())
    total = min(total, 100)

    return ScoreResult(
        score=total,
        tier=_tier(total, tiers),
        breakdown=breakdown,
        disqualified_by=[],
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    # Quick smoke-test with dummy data.
    c = Company(
        company_number="12345678",
        company_name="Acme IT Solutions Ltd",
        company_status="active",
        date_of_creation="2010-06-01",
        sic_codes=["62020"],
        region="West Midlands",
    )
    o = Owner(name="John Smith", kind="individual", control_pct=87, age=58)
    result = score_company(
        c, o,
        employee_count=22,
        msp_label="msp",
        msp_confidence=90,
        recurring_rev_confidence=80,
    )
    print(f"Score: {result.score}  Tier: {result.tier}")
    print("Breakdown:", result.breakdown)
