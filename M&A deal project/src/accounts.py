"""
accounts.py
Pull filed accounts data from Companies House to estimate company size.

Companies House filing history exposes the most-recent annual accounts type
(micro-entity, small, total-exemption-small, etc.) which is a free proxy for
revenue band — useful when no Apollo employee count is available.

Deps: requests (already installed via companies_house.py)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .companies_house import _get

logger = logging.getLogger(__name__)

# Maps CH accounts category → rough revenue band string (heuristic for MSPs).
_ACCOUNTS_SIZE_MAP = {
    "micro-entity": "< £632k turnover",
    "total-exemption-small": "< £10.2M turnover (small)",
    "small": "< £10.2M turnover (small)",
    "group": "group accounts — subsidiary risk",
    "full": "> £10.2M turnover (medium/large)",
    "medium": "> £10.2M turnover",
    "dormant": "dormant — disqualify",
}


@dataclass
class AccountsInfo:
    company_number: str
    accounts_category: Optional[str]
    last_accounts_date: Optional[str]
    next_due_date: Optional[str]
    revenue_band_hint: str
    is_overdue: bool


def get_accounts_info(company_number: str) -> AccountsInfo:
    """Fetch the company profile's accounts block for size classification."""
    profile = _get(f"/company/{company_number}")
    accounts_block = profile.get("accounts", {}) or {}
    last_accounts = accounts_block.get("last_accounts", {}) or {}

    category = last_accounts.get("type")            # e.g. "micro-entity", "small"
    made_up_to = last_accounts.get("made_up_to")    # e.g. "2023-03-31"
    next_due = accounts_block.get("next_due")
    overdue = accounts_block.get("overdue", False)

    band = _ACCOUNTS_SIZE_MAP.get(category or "", "unknown")

    return AccountsInfo(
        company_number=company_number,
        accounts_category=category,
        last_accounts_date=made_up_to,
        next_due_date=next_due,
        revenue_band_hint=band,
        is_overdue=overdue,
    )


def get_filing_history(company_number: str, category: str = "accounts", items_per_page: int = 5) -> list[dict]:
    """Return recent filings of a given category (default: accounts)."""
    data = _get(
        f"/company/{company_number}/filing-history",
        params={"category": category, "items_per_page": items_per_page},
    )
    return data.get("items", []) if data else []


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    cn = sys.argv[1] if len(sys.argv) > 1 else "00041424"
    info = get_accounts_info(cn)
    print(info)
