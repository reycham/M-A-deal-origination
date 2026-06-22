"""
run.py
End-to-end M&A deal origination pipeline orchestrator.

Usage:
  python run.py                          # use config/thesis.yaml defaults
  python run.py --region "North West"    # override region
  python run.py --max 300 --no-email     # cap companies, skip email enrichment
  python run.py --skip-llm               # skip LLM classification (faster demo)

Pipeline:
  1. Source companies from Companies House (SIC + region)
  2. Resolve PSC ownership — drop corporate-owned
  3. Pull accounts size data
  4. [Optional] LLM classify (MSP + recurring rev)
  5. [Optional] Enrich owner email (Prospeo → Icypeas → MillionVerifier)
  6. Score + tier (thesis.yaml weights)
  7. Export to data/final/  + print summary

Checkpointing: raw and interim data saved to data/raw/ and data/interim/
so you can re-run from a later stage without re-hitting the CH API.

Deps: pip install requests anthropic beautifulsoup4 pyyaml python-dotenv
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

from src.companies_house import search_companies, Company
from src.psc import resolve_owner, Owner, succession_signal
from src.accounts import get_accounts_info
from src.apollo import enrich as apollo_enrich
from src.classify_llm import classify_company
from src.enrich_email import find_and_verify_email
from src.fit_score import score_company
from src.merge_dedupe import build_enriched, dedupe, EnrichedCompany
from src.export import to_csv, summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_THESIS_PATH = Path(__file__).parent / "config" / "thesis.yaml"
_DATA_RAW = Path(__file__).parent / "data" / "raw"
_DATA_INTERIM = Path(__file__).parent / "data" / "interim"


def _save_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def run(
    region_override: str | None = None,
    max_companies: int = 500,
    skip_llm: bool = False,
    skip_email: bool = False,
    thesis_path: Path = _THESIS_PATH,
) -> list[EnrichedCompany]:
    thesis = yaml.safe_load(thesis_path.read_text(encoding="utf-8"))
    sic_codes = [str(s) for s in thesis["sic_codes"]]
    regions = [region_override] if region_override else thesis.get("regions", [None])
    min_years = thesis.get("min_years_in_business", 10)

    cutoff = (
        datetime.date.today() - datetime.timedelta(days=365 * min_years)
    ).isoformat()

    # ── Step 1: Source ────────────────────────────────────────────────────────
    all_companies: list[Company] = []
    for region in regions:
        logger.info("Sourcing CH companies — region=%s", region or "all UK")
        batch = search_companies(
            sic_codes=sic_codes,
            location=region,
            incorporated_before=cutoff,
            max_results=max_companies // max(len(regions), 1),
        )
        logger.info("  Found %d candidates", len(batch))
        all_companies.extend(batch)

    _save_json(
        [vars(c) for c in all_companies],
        _DATA_RAW / "companies_raw.json",
    )
    logger.info("Total raw companies: %d", len(all_companies))

    # ── Step 2: Resolve PSC ownership ────────────────────────────────────────
    records: list[EnrichedCompany] = []
    dropped_corporate = 0

    for i, company in enumerate(all_companies, 1):
        if i % 50 == 0:
            logger.info("PSC resolution: %d / %d", i, len(all_companies))

        owner: Owner = resolve_owner(company.company_number)

        if owner.kind == "corporate":
            dropped_corporate += 1
            # Still include in records so we can show disqualification
        if owner.kind == "none":
            logger.debug("No PSC for %s — keeping with unknown owner", company.company_number)

        rec = build_enriched(company, owner)
        records.append(rec)

    logger.info("Dropped %d corporate-owned companies", dropped_corporate)
    records = dedupe(records)

    _save_json(
        [r.to_dict() for r in records],
        _DATA_INTERIM / "after_psc.json",
    )

    # ── Step 3: Accounts size data ────────────────────────────────────────────
    logger.info("Pulling accounts data...")
    for r in records:
        try:
            info = get_accounts_info(r.company_number)
            r.accounts_category = info.accounts_category
            r.revenue_band_hint = info.revenue_band_hint
            r.last_accounts_date = info.last_accounts_date
        except Exception as exc:
            logger.debug("Accounts fetch failed for %s: %s", r.company_number, exc)

    # ── Step 4: Website URL — guess domain + verify (zero cost fallback) ────────
    from src.apollo import _guess_domain
    from src.classify_llm import fetch_website_text
    import requests as _req
    logger.info("Resolving website URLs...")
    for r in records:
        if r.website_url:
            continue
        domain = r.domain or _guess_domain(r.company_name)
        if not domain:
            continue
        # Try .co.uk then .com — just check the site responds before storing
        for candidate in [f"https://{domain}", f"https://{domain.replace('.co.uk', '.com')}"]:
            try:
                head = _req.head(candidate, timeout=5, allow_redirects=True)
                if head.status_code < 400:
                    r.website_url = candidate
                    r.domain = domain
                    break
            except Exception:
                continue

    # ── Step 5: LLM Classification (uses website URL if resolved) ────────────
    if not skip_llm:
        logger.info("Running LLM classification (%d companies)...", len(records))
        for i, r in enumerate(records, 1):
            if i % 20 == 0:
                logger.info("  LLM: %d / %d", i, len(records))
            try:
                llm = classify_company(
                    company_name=r.company_name,
                    sic_codes=r.sic_codes,
                    website_url=r.website_url or "",
                    description=r.description or "",
                )
                r.msp_label = llm["msp_label"]
                r.msp_confidence = llm["msp_confidence"]
                r.msp_evidence = llm["msp_evidence"]
                r.msp_reasoning = llm["msp_reasoning"]
                r.recurring_rev_confidence = llm["recurring_rev_confidence"]
                r.recurring_rev_guess = llm["recurring_rev_guess"]
            except Exception as exc:
                logger.warning("LLM failed for %s: %s", r.company_name, exc)
    else:
        logger.info("Skipping LLM classification (--skip-llm)")
        for r in records:
            r.msp_label = "unclear"
            r.recurring_rev_confidence = 50

    _save_json(
        [r.to_dict() for r in records],
        _DATA_INTERIM / "after_llm.json",
    )

    # ── Step 5: Email Enrichment ──────────────────────────────────────────────
    if not skip_email:
        # Only enrich individual-owned companies (not disqualified yet)
        to_enrich = [r for r in records if r.owner_kind == "individual" and r.owner_name]
        logger.info("Email enrichment for %d founder-owned companies...", len(to_enrich))
        for i, r in enumerate(to_enrich, 1):
            if i % 20 == 0:
                logger.info("  Email: %d / %d", i, len(to_enrich))
            try:
                email_result = find_and_verify_email(
                    owner_name=r.owner_name,
                    company_name=r.company_name,
                    domain=r.domain,
                )
                r.owner_email = email_result.email
                r.email_source = email_result.source
                r.email_mv_status = email_result.mv_status
                r.email_is_usable = email_result.is_usable
            except Exception as exc:
                logger.warning("Email enrich failed for %s: %s", r.company_name, exc)
    else:
        logger.info("Skipping email enrichment (--no-email)")

    # ── Step 6: Fit Scoring ───────────────────────────────────────────────────
    logger.info("Scoring companies...")
    for r in records:
        try:
            result = score_company(
                company=Company(
                    company_number=r.company_number,
                    company_name=r.company_name,
                    company_status=r.company_status,
                    date_of_creation=r.incorporation_date,
                    sic_codes=r.sic_codes,
                    region=r.region,
                ),
                owner=Owner(
                    name=r.owner_name,
                    kind=r.owner_kind,
                    control_pct=r.owner_control_pct,
                    age=r.owner_age,
                    nationality=r.owner_nationality,
                ),
                employee_count=r.employee_count,
                has_funding=r.has_funding,
                msp_label=r.msp_label,
                msp_confidence=r.msp_confidence,
                recurring_rev_confidence=r.recurring_rev_confidence,
                thesis_path=thesis_path,
            )
            r.fit_score = result.score
            r.tier = result.tier
            r.score_breakdown = result.breakdown
            r.disqualified_by = result.disqualified_by
        except Exception as exc:
            logger.warning("Scoring failed for %s: %s", r.company_name, exc)

    # ── Step 7: Export ────────────────────────────────────────────────────────
    summary(records)
    to_csv(records)

    return records


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M&A deal origination pipeline")
    p.add_argument("--region", default=None, help="Override region (e.g. 'North West')")
    p.add_argument("--max", type=int, default=500, dest="max_companies",
                   help="Max companies to source (default 500)")
    p.add_argument("--skip-llm", action="store_true", help="Skip LLM classification")
    p.add_argument("--no-email", action="store_true", dest="skip_email",
                   help="Skip email enrichment")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(
        region_override=args.region,
        max_companies=args.max_companies,
        skip_llm=args.skip_llm,
        skip_email=args.skip_email,
    )
