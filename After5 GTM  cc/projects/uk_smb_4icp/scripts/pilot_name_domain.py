"""Pilot: re-run 100 NO_MATCH rows via Prospeo using name + company domain
(instead of LinkedIn URL) with only_verified_email=False to see whether the
name+domain index has data the linkedin-url path missed.
"""
from __future__ import annotations

import os
import sys
import time
import json
import re
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
ENRICHED_DIR = ROOT / "output" / "enriched"
OUT_PATH = ENRICHED_DIR / "_pilot_name_domain.csv"

API_URL = "https://api.prospeo.io/enrich-person"
API_KEY = os.environ.get("PROSPEO_API_KEY")
if not API_KEY:
    sys.exit("Set PROSPEO_API_KEY env var.")

SAMPLE_SIZE = 100
REQUEST_INTERVAL_SEC = 0.5
TIMEOUT_SEC = 30


def domain_from_url(u: str) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = u.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    return s.split("/", 1)[0].split("?", 1)[0]


def call(first: str, last: str, domain: str) -> dict:
    payload = {
        "only_verified_email": False,
        "enrich_mobile": False,
        "data": {"first_name": first, "last_name": last, "company_website": domain},
    }
    headers = {"X-KEY": API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC)
    except requests.RequestException as e:
        return {"status": "network_error", "email": "", "email_status": "",
                "http_code": 0, "raw_error": str(e)[:200]}
    try:
        body = r.json()
    except ValueError:
        return {"status": "bad_json", "email": "", "email_status": "",
                "http_code": r.status_code, "raw_error": r.text[:200]}
    if r.status_code != 200 or body.get("error"):
        return {"status": "no_match", "email": "", "email_status": "",
                "http_code": r.status_code, "raw_error": json.dumps(body)[:200]}
    person = body.get("response", body).get("person") or body.get("person") or {}
    em = person.get("email") or {}
    email = em.get("email") if em.get("revealed") else ""
    return {"status": "found" if email else "no_email",
            "email": email, "email_status": em.get("status") or "",
            "http_code": 200, "raw_error": ""}


def main() -> None:
    # Load run log; previous "api_error" rows are NO_MATCH from LinkedIn-keyed run
    log = pd.read_csv(ENRICHED_DIR / "_prospeo_run_log.csv", dtype=str, keep_default_na=False)
    no_match = log[log["status"] == "api_error"].copy()
    print(f"Total NO_MATCH from previous run: {len(no_match)}")

    # Stratified sample across the 8 (icp, persona) buckets
    no_match["_bucket"] = no_match["icp"] + " / " + no_match["persona"]
    sample = no_match.groupby("_bucket", group_keys=False).apply(
        lambda g: g.sample(min(len(g), max(1, SAMPLE_SIZE * len(g) // len(no_match))), random_state=42)
    )
    if len(sample) < SAMPLE_SIZE:
        # Top up to SAMPLE_SIZE
        extra = no_match.drop(sample.index).sample(SAMPLE_SIZE - len(sample), random_state=42)
        sample = pd.concat([sample, extra])
    sample = sample.head(SAMPLE_SIZE).reset_index(drop=True)
    print(f"Pilot sample: {len(sample)} rows")
    print(sample["_bucket"].value_counts().to_string())

    # Pull first/last/website from the enriched CSVs by joining on linkedin_url
    enrich_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def lookup_row(icp: str, persona: str, li: str) -> dict:
        key = (icp, persona)
        if key not in enrich_cache:
            path = ENRICHED_DIR / f"{icp} - {persona} enriched.csv"
            enrich_cache[key] = pd.read_csv(path, dtype=str, keep_default_na=False)
        df = enrich_cache[key]
        m = df[df[f"{persona}_linkedin"] == li]
        if m.empty:
            return {}
        r = m.iloc[0]
        return {
            "first_name": r.get(f"{persona}_first_name", ""),
            "last_name": r.get(f"{persona}_last_name", ""),
            "website": r.get("Website", ""),
            "company": r.get("Company Name", ""),
        }

    results = []
    found = 0
    for i, row in sample.iterrows():
        info = lookup_row(row["icp"], row["persona"], row["linkedin_url"])
        first = info.get("first_name", "").strip()
        last = info.get("last_name", "").strip()
        domain = domain_from_url(info.get("website", ""))

        if not (first and last and domain):
            res = {"status": "skipped_no_input", "email": "", "email_status": "",
                   "http_code": 0, "raw_error": "missing first/last/domain"}
        else:
            res = call(first, last, domain)

        results.append({
            "icp": row["icp"], "persona": row["persona"],
            "company": info.get("company", ""), "first_name": first, "last_name": last,
            "domain": domain, "linkedin_url": row["linkedin_url"],
            "status": res["status"], "email": res["email"],
            "email_status": res["email_status"],
            "http_code": res["http_code"], "raw_error": res["raw_error"],
        })
        if res["status"] == "found":
            found += 1
        if (i + 1) % 20 == 0:
            print(f"  ...{i+1} calls, {found} found")
            pd.DataFrame(results).to_csv(OUT_PATH, index=False)
        time.sleep(REQUEST_INTERVAL_SEC)

    pd.DataFrame(results).to_csv(OUT_PATH, index=False)
    print(f"\nDone. {found}/{len(sample)} found ({100*found/len(sample):.1f}%)")
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
