"""One-shot fix: re-write `personalized_opener` for every lead already in the
4 Smartlead campaigns, substituting `{{company_short}}` with the actual brand.

Smartlead does not recursively expand placeholders inside custom-field values,
so the original openers like "Came across {{company_short}} today and ..."
rendered as "Came across  today and ...". This script:

  1. For each campaign in `output/smartlead/_campaign_ids.json`, paginate
     `GET /campaigns/{id}/leads` to collect (lead_id, email).
  2. Match each lead's email to the per-ICP lead CSV to find the correct
     resolved opener (already pre-substituted by the patched script 17 if
     re-run, or substituted inline here from the master file).
  3. POST `/campaigns/{id}/leads/{lead_id}` with the corrected
     `custom_fields.personalized_opener`.

Default mode is dry-run (no writes). Pass --push to apply.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.config import get_key  # noqa: E402

OUT_DIR = PROJECT_ROOT / "output" / "smartlead"
MASTER_CSV = PROJECT_ROOT / "output" / "final_v6" / "_all_deliverable_ranked.csv"
SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"


def build_email_to_opener() -> dict[str, str]:
    df = pd.read_csv(MASTER_CSV, dtype=str, keep_default_na=False)
    out: dict[str, str] = {}
    for _, r in df.iterrows():
        email = r["final_email"].strip().lower()
        if not email:
            continue
        opener = r["personalized_opener"].replace("{{company_short}}", r["company_short"])
        out[email] = opener
    return out


def fetch_leads(api_key: str, campaign_id: int) -> list[dict]:
    """Paginates GET /campaigns/{id}/leads. Returns list of {id, email, ...}."""
    s = requests.Session()
    out: list[dict] = []
    offset = 0
    limit = 100
    while True:
        url = (f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads"
               f"?api_key={api_key}&offset={offset}&limit={limit}")
        r = s.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        # Smartlead returns {"data": [...], "total_leads": N} based on docs.
        page = data.get("data") or data.get("leads") or (data if isinstance(data, list) else [])
        if not page:
            break
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(0.2)
    return out


def update_lead(api_key: str, campaign_id: int, lead_id: int, email: str, opener: str) -> None:
    url = f"{SMARTLEAD_BASE}/campaigns/{campaign_id}/leads/{lead_id}?api_key={api_key}"
    body = {"email": email, "custom_fields": {"personalized_opener": opener}}
    r = requests.post(url, json=body, timeout=30)
    r.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--icp", type=str, default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    api_key = get_key("SMARTLEAD_API_KEY")
    ids_path = OUT_DIR / "_campaign_ids.json"
    if not ids_path.exists():
        sys.exit(f"Missing {ids_path} — run script 17 --push first.")
    campaign_ids: dict[str, int] = json.loads(ids_path.read_text())

    print("Building email -> resolved-opener lookup from master CSV...")
    email_to_opener = build_email_to_opener()
    print(f"  {len(email_to_opener)} email->opener mappings.\n")

    icp_filter = [args.icp] if args.icp else list(campaign_ids.keys())

    grand_updated = 0
    grand_skipped = 0
    for icp in icp_filter:
        cid = campaign_ids.get(icp)
        if not cid:
            print(f"[skip] {icp}: no campaign id")
            continue
        print(f"=== {icp} (campaign {cid}) ===")

        # The lead nesting in the response varies; nodes from /leads endpoint
        # are usually wrapped: {data: [{lead: {...lead fields...}, ...}, ...]}.
        # We tolerate both.
        raw_leads = fetch_leads(api_key, cid)
        print(f"  fetched {len(raw_leads)} leads from Smartlead")

        # Normalise: each entry should have a lead_id and email.
        norm: list[tuple[int, str]] = []
        for entry in raw_leads:
            inner = entry.get("lead", entry) if isinstance(entry, dict) else {}
            lead_id = inner.get("id") or entry.get("lead_id") or entry.get("id")
            email = (inner.get("email") or entry.get("email") or "").strip().lower()
            if lead_id and email:
                norm.append((int(lead_id), email))

        if args.limit:
            norm = norm[: args.limit]

        updated = 0
        skipped = 0
        for i, (lead_id, email) in enumerate(norm, 1):
            opener = email_to_opener.get(email)
            if not opener or "{{company_short}}" in opener:
                skipped += 1
                continue
            if not args.push:
                if i <= 3:
                    print(f"  [dry] would update lead {lead_id} ({email}): {opener[:80]}")
                updated += 1
                continue
            try:
                update_lead(api_key, cid, lead_id, email, opener)
                updated += 1
                if updated % 50 == 0:
                    print(f"  updated {updated}/{len(norm)}")
            except Exception as e:
                print(f"  !! failed lead {lead_id} ({email}): {e}")
                skipped += 1
            time.sleep(0.15)

        print(f"  {icp}: updated={updated}, skipped={skipped}\n")
        grand_updated += updated
        grand_skipped += skipped

    print(f"TOTAL updated={grand_updated}, skipped={grand_skipped}")
    if not args.push:
        print("Dry-run only. Pass --push to actually update leads.")


if __name__ == "__main__":
    main()
