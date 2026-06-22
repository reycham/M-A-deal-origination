"""
apollo.py
Enrich companies via Apollo's Organization Enrich API.

Returns: employee count, website URL, description, industry, funding status.
These feed directly into fit_score (size_band) and classify_llm (website text).

Docs: https://apolloio.github.io/apollo-api-docs/#organization-enrichment
Rate limit: free tier ~50 enrichments/month, ~1 req/sec.

Deps: pip install requests
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
_ENRICH_URL = "https://api.apollo.io/v1/organizations/enrich"
_MIN_INTERVAL = 1.2  # stay under 1 req/sec
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


@dataclass
class ApolloOrg:
    domain: Optional[str]
    website_url: Optional[str]
    employee_count: Optional[int]
    description: Optional[str]
    industry: Optional[str]
    has_funding: bool


def _guess_domain(company_name: str) -> str:
    """Derive a likely .co.uk domain from a UK company name."""
    name = company_name.lower()
    for suffix in [
        " limited", " ltd", " llp", " plc", " group", " holdings",
        " uk", " solutions", " services", " technologies", " consulting",
        " consultants", " systems", " associates", " computing",
    ]:
        name = name.replace(suffix, "")
    name = re.sub(r"[^a-z0-9]", "", name.strip())
    return f"{name}.co.uk" if name else ""


def enrich(company_name: str, domain: str | None = None) -> ApolloOrg:
    """
    Enrich a company by domain (preferred) or name.
    Returns ApolloOrg with available fields; missing fields are None/False.
    """
    if not APOLLO_API_KEY:
        logger.debug("APOLLO_API_KEY not set — skipping Apollo enrichment")
        return ApolloOrg(None, None, None, None, None, False)

    target_domain = domain or _guess_domain(company_name)

    payload: dict = {}
    if target_domain:
        payload["domain"] = target_domain
    else:
        payload["name"] = company_name

    _throttle()
    try:
        resp = requests.post(
            _ENRICH_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_API_KEY,
            },
            timeout=15,
        )
        if resp.status_code == 404:
            logger.debug("Apollo: no result for %s", target_domain or company_name)
            return ApolloOrg(None, None, None, None, None, False)
        resp.raise_for_status()
        org = resp.json().get("organization") or {}

        employee_count = org.get("estimated_num_employees")
        has_funding = bool(org.get("num_funding_rounds", 0))

        return ApolloOrg(
            domain=org.get("primary_domain") or target_domain,
            website_url=org.get("website_url"),
            employee_count=int(employee_count) if employee_count else None,
            description=org.get("short_description"),
            industry=org.get("industry"),
            has_funding=has_funding,
        )
    except Exception as exc:
        logger.debug("Apollo enrich failed for %s: %s", company_name, exc)
        return ApolloOrg(None, None, None, None, None, False)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    name = sys.argv[1] if len(sys.argv) > 1 else "Softcat Ltd"
    result = enrich(name)
    print(result)
