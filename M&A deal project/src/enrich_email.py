"""
email.py
Owner email enrichment waterfall: Prospeo → Icypeas → MillionVerifier verify.

Waterfall logic:
  1. Prospeo email-finder (name + domain)
  2. If no result → Icypeas email-search (name + domain)
  3. If found → MillionVerifier single-check (status: ok / catch_all / invalid)

Only returns verified or catch_all emails; invalid/unknown are discarded.

Deps: pip install requests
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

PROSPEO_API_KEY = os.environ.get("PROSPEO_API_KEY", "")
ICYPEAS_API_KEY = os.environ.get("ICYPEAS_API_KEY", "")
MV_API_KEY = os.environ.get("MILLIONVERIFIER_API_KEY", "")


@dataclass
class EmailResult:
    email: Optional[str]
    source: str           # "prospeo" | "icypeas" | "none"
    mv_status: str        # "ok" | "catch_all" | "invalid" | "unknown" | "not_checked"
    is_usable: bool       # True for ok / catch_all


def _domain_from_name(company_name: str) -> str:
    """Very rough heuristic: strip legal suffixes, lowercase, remove spaces."""
    name = company_name.lower()
    for suffix in [" limited", " ltd", " llp", " plc", " group", " holdings", " uk"]:
        name = name.replace(suffix, "")
    name = re.sub(r"[^a-z0-9]", "", name)
    return f"{name}.co.uk"


# ── Prospeo ──────────────────────────────────────────────────────────────────

def _prospeo_find(first_name: str, last_name: str, domain: str) -> Optional[str]:
    if not PROSPEO_API_KEY:
        logger.debug("PROSPEO_API_KEY not set — skipping")
        return None
    try:
        resp = requests.post(
            "https://api.prospeo.io/email-finder",
            json={"first_name": first_name, "last_name": last_name, "company": domain},
            headers={"X-KEY": PROSPEO_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        email = data.get("response", {}).get("email")
        return email if email else None
    except Exception as exc:
        logger.debug("Prospeo error: %s", exc)
        return None


# ── Icypeas ───────────────────────────────────────────────────────────────────

def _icypeas_find(first_name: str, last_name: str, domain: str) -> Optional[str]:
    if not ICYPEAS_API_KEY:
        logger.debug("ICYPEAS_API_KEY not set — skipping")
        return None
    try:
        resp = requests.post(
            "https://app.icypeas.com/api/email-search",
            json={"firstname": first_name, "lastname": last_name, "domainOrCompany": domain},
            headers={"Authorization": ICYPEAS_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("item", {}).get("emails", [])
        return items[0] if items else None
    except Exception as exc:
        logger.debug("Icypeas error: %s", exc)
        return None


# ── MillionVerifier ───────────────────────────────────────────────────────────

def _mv_verify(email: str) -> str:
    """Returns: ok | catch_all | invalid | unknown"""
    if not MV_API_KEY:
        return "not_checked"
    try:
        resp = requests.get(
            "https://api.millionverifier.com/api/v3/",
            params={"api": MV_API_KEY, "email": email},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", "unknown")
    except Exception as exc:
        logger.debug("MillionVerifier error: %s", exc)
        return "unknown"


# ── Public API ────────────────────────────────────────────────────────────────

def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def find_and_verify_email(
    owner_name: str,
    company_name: str,
    domain: Optional[str] = None,
) -> EmailResult:
    """
    Waterfall: Prospeo → Icypeas → MillionVerifier.
    domain: pass if known (e.g. from Apollo); else derived from company_name.
    """
    if not owner_name:
        return EmailResult(email=None, source="none", mv_status="not_checked", is_usable=False)

    first, last = _split_name(owner_name)
    target_domain = domain or _domain_from_name(company_name)

    email = _prospeo_find(first, last, target_domain)
    source = "prospeo" if email else ""

    if not email:
        email = _icypeas_find(first, last, target_domain)
        source = "icypeas" if email else "none"

    if not email:
        return EmailResult(email=None, source="none", mv_status="not_checked", is_usable=False)

    mv_status = _mv_verify(email)
    is_usable = mv_status in ("ok", "catch_all")

    if not is_usable:
        logger.info("Email %s returned mv_status=%s — marking unusable", email, mv_status)

    return EmailResult(email=email, source=source, mv_status=mv_status, is_usable=is_usable)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    name = sys.argv[1] if len(sys.argv) > 1 else "John Smith"
    company = sys.argv[2] if len(sys.argv) > 2 else "Acme IT Solutions Ltd"
    result = find_and_verify_email(name, company)
    print(result)
