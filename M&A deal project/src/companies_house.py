"""
companies_house.py
Source UK companies from the Companies House Public Data API by SIC code + region.

Auth   : HTTP Basic — API key as the username, blank password.
Key    : free at https://developer.company-information.service.gov.uk/
Limits : 600 requests / 5 min. This client self-throttles and retries on 429.
Deps   : pip install requests
"""
from __future__ import annotations

import os
import time
import logging
import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.company-information.service.gov.uk"
API_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
_CURRENT_YEAR = _dt.date.today().year

# 600 req / 5 min ≈ 2 req/sec. Stay comfortably under it.
_MIN_INTERVAL = 0.6
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _get(path: str, params: dict | None = None, max_retries: int = 4) -> dict:
    """Throttled GET against the CH API with exponential backoff on 429."""
    if not API_KEY:
        raise RuntimeError("Set COMPANIES_HOUSE_API_KEY in your environment / .env")
    url = f"{BASE_URL}{path}"
    for attempt in range(max_retries):
        _throttle()
        resp = requests.get(url, params=params, auth=(API_KEY, ""), timeout=30)
        if resp.status_code == 429:
            backoff = 2 ** attempt
            logger.warning("Rate limited; backing off %ss", backoff)
            time.sleep(backoff)
            continue
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Gave up on {url} after {max_retries} retries")


@dataclass
class Company:
    company_number: str
    company_name: str
    company_status: str
    date_of_creation: Optional[str]
    sic_codes: list[str] = field(default_factory=list)
    region: Optional[str] = None
    locality: Optional[str] = None
    postal_code: Optional[str] = None

    @property
    def years_in_business(self) -> Optional[int]:
        if not self.date_of_creation:
            return None
        return _CURRENT_YEAR - int(self.date_of_creation[:4])


def search_companies(
    sic_codes: list[str],
    location: str | None = None,
    *,
    active_only: bool = True,
    incorporated_before: str | None = None,   # "YYYY-MM-DD" → enforces "X+ years old"
    page_size: int = 100,
    max_results: int = 1000,
) -> list[Company]:
    """
    Advanced search for companies matching the given SIC codes.

    sic_codes           : e.g. ["62020", "62030", "62090", "62012", "95110"]
    location            : registered-office text, e.g. "Manchester"
    incorporated_before : keep only companies created on/before this date

    Note: the advanced-search `sic_codes` param accepts a comma-separated list.
    If your account ever returns odd results, loop one SIC at a time and dedupe
    on company_number instead.
    """
    params: dict = {
        "sic_codes": ",".join(sic_codes),
        "size": page_size,
        "start_index": 0,
    }
    if location:
        params["location"] = location
    if active_only:
        params["company_status"] = "active"
    if incorporated_before:
        params["incorporated_to"] = incorporated_before

    results: list[Company] = []
    while len(results) < max_results:
        data = _get("/advanced-search/companies", params)
        items = data.get("items", [])
        if not items:
            break
        for it in items:
            addr = it.get("registered_office_address", {}) or {}
            results.append(
                Company(
                    company_number=it.get("company_number", ""),
                    company_name=it.get("company_name", ""),
                    company_status=it.get("company_status", ""),
                    date_of_creation=it.get("date_of_creation"),
                    sic_codes=it.get("sic_codes", []) or [],
                    region=addr.get("region"),
                    locality=addr.get("locality"),
                    postal_code=addr.get("postal_code"),
                )
            )
        if len(items) < page_size:
            break
        params["start_index"] += page_size
    return results[:max_results]


def get_company_profile(company_number: str) -> dict:
    """Full profile — incorporation date, accounts category, SIC, status."""
    return _get(f"/company/{company_number}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # UK MSP thesis: IT-services SIC codes, 10+ years old, active, Manchester.
    ten_years_ago = (_dt.date.today() - _dt.timedelta(days=365 * 10)).isoformat()
    msps = search_companies(
        sic_codes=["62020", "62030", "62090", "62012", "95110"],
        location="Manchester",
        incorporated_before=ten_years_ago,
        max_results=200,
    )
    print(f"Found {len(msps)} candidates")
    for c in msps[:10]:
        print(f"{c.company_number}  {c.company_name}  ({c.years_in_business}y)  {c.region}")
