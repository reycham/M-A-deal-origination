"""
export.py
Write the scored target list to CSV in data/final/.

Output columns match the Clay table spec from the deal-origination spec.
Sorted by fit_score descending; A/B tiers first.
"""
from __future__ import annotations

import csv
import datetime
import logging
from pathlib import Path

from .merge_dedupe import EnrichedCompany

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).parent.parent / "data" / "final"

# Ordered columns for the CSV (Clay-compatible headers).
_COLUMNS = [
    "tier", "fit_score", "company_name", "company_number",
    "region", "locality", "postal_code",
    "incorporation_date", "years_in_business",
    "sic_codes", "company_status",
    "owner_name", "owner_kind", "owner_control_pct", "owner_age", "owner_nationality",
    "is_founder_owned", "employee_count", "estimated_revenue",
    "accounts_category", "revenue_band_hint", "last_accounts_date",
    "has_funding", "msp_label", "msp_confidence", "msp_reasoning",
    "recurring_rev_confidence", "recurring_rev_guess",
    "website_url", "domain", "description",
    "owner_email", "email_source", "email_mv_status", "email_is_usable",
    "score_breakdown", "disqualified_by",
]


def _prep_row(r: EnrichedCompany) -> dict:
    d = r.to_dict()
    d["sic_codes"] = ", ".join(d.get("sic_codes") or [])
    d["estimated_revenue"] = d.get("revenue_band_hint", "")
    return d


def to_csv(
    records: list[EnrichedCompany],
    filename: str | None = None,
    tiers: tuple[str, ...] = ("A", "B", "C"),
) -> Path:
    """
    Write scored records to CSV. Returns the file path.
    Filters to the given tiers (default: all). Sorts by score desc.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not filename:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"msp_targets_{stamp}.csv"

    out_path = _OUTPUT_DIR / filename

    filtered = [r for r in records if r.tier in tiers]
    sorted_records = sorted(filtered, key=lambda r: r.fit_score, reverse=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted_records:
            writer.writerow(_prep_row(r))

    logger.info("Exported %d records -> %s", len(sorted_records), out_path)
    print(f"\nExported {len(sorted_records)} records -> {out_path}")
    return out_path


def summary(records: list[EnrichedCompany]) -> None:
    """Print a quick summary table to stdout."""
    a = [r for r in records if r.tier == "A"]
    b = [r for r in records if r.tier == "B"]
    c = [r for r in records if r.tier == "C"]
    dq = [r for r in records if r.disqualified_by]

    print("\n-- Target List Summary ------------------------------------------")
    print(f"  A-tier  (>=80) : {len(a):>4}")
    print(f"  B-tier  (>=60) : {len(b):>4}")
    print(f"  C-tier  (<60)  : {len(c):>4}")
    print(f"  Disqualified   : {len(dq):>4}")
    print(f"  Total          : {len(records):>4}")
    print("-----------------------------------------------------------------")

    if a or b:
        print("\nTop 10 A/B targets:")
        top = sorted(a + b, key=lambda r: r.fit_score, reverse=True)[:10]
        for r in top:
            email_flag = "✓" if r.email_is_usable else "–"
            print(
                f"  [{r.tier}] {r.fit_score:>3}  {r.company_name:<40} "
                f"owner: {r.owner_name or '?':<25} email: {email_flag}"
            )
