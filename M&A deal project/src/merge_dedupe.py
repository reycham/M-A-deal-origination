"""
merge_dedupe.py
Consolidate enrichment data from multiple sources into one record per company.
Deduplicate on company_number (authoritative) then domain.

This is where you'd merge Apollo / Crunchbase / Apify data with the
Companies House base. For now it handles the CH + PSC + accounts + LLM layer
that this repo actually runs — the Apollo/Crunchbase fields are optional extras.

Deps: none beyond stdlib
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from typing import Optional

from .companies_house import Company
from .psc import Owner

logger = logging.getLogger(__name__)


@dataclass
class EnrichedCompany:
    # Companies House base
    company_number: str
    company_name: str
    company_status: str
    sic_codes: list[str]
    region: Optional[str]
    locality: Optional[str]
    postal_code: Optional[str]
    incorporation_date: Optional[str]
    years_in_business: Optional[int]

    # PSC / ownership
    owner_name: Optional[str]
    owner_kind: str
    owner_control_pct: Optional[int]
    owner_age: Optional[int]
    owner_nationality: Optional[str]
    is_founder_owned: bool

    # Accounts / size
    accounts_category: Optional[str] = None
    revenue_band_hint: str = "unknown"
    last_accounts_date: Optional[str] = None

    # External enrichment (Apollo/Crunchbase — optional)
    employee_count: Optional[int] = None
    description: Optional[str] = None
    website_url: Optional[str] = None
    domain: Optional[str] = None
    has_funding: bool = False

    # LLM classification
    msp_label: str = "unclear"
    msp_confidence: int = 0
    msp_evidence: list[str] = field(default_factory=list)
    msp_reasoning: str = ""
    recurring_rev_confidence: int = 0
    recurring_rev_guess: str = "unclear"

    # Email enrichment
    owner_email: Optional[str] = None
    email_source: str = "none"
    email_mv_status: str = "not_checked"
    email_is_usable: bool = False

    # Scoring
    fit_score: int = 0
    tier: str = "C"
    score_breakdown: dict = field(default_factory=dict)
    disqualified_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["msp_evidence"] = "; ".join(d["msp_evidence"])
        d["score_breakdown"] = str(d["score_breakdown"])
        d["disqualified_by"] = "; ".join(d["disqualified_by"])
        return d


def build_enriched(company: Company, owner: Owner) -> EnrichedCompany:
    return EnrichedCompany(
        company_number=company.company_number,
        company_name=company.company_name,
        company_status=company.company_status,
        sic_codes=company.sic_codes,
        region=company.region,
        locality=company.locality,
        postal_code=company.postal_code,
        incorporation_date=company.date_of_creation,
        years_in_business=company.years_in_business,
        owner_name=owner.name,
        owner_kind=owner.kind,
        owner_control_pct=owner.control_pct,
        owner_age=owner.age,
        owner_nationality=owner.nationality,
        is_founder_owned=owner.is_founder_owned,
    )


def dedupe(records: list[EnrichedCompany]) -> list[EnrichedCompany]:
    """Deduplicate on company_number, then on domain."""
    seen_numbers: set[str] = set()
    seen_domains: set[str] = set()
    out: list[EnrichedCompany] = []

    for r in records:
        if r.company_number and r.company_number in seen_numbers:
            continue
        if r.domain and r.domain in seen_domains:
            continue
        if r.company_number:
            seen_numbers.add(r.company_number)
        if r.domain:
            seen_domains.add(r.domain)
        out.append(r)

    before, after = len(records), len(out)
    if before != after:
        logger.info("Deduped %d → %d records", before, after)
    return out
