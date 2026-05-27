"""Enrich missing emails on matched DMs via Prospeo's enrich-person API.

Inputs:  output/missing_emails/<ICP> - <persona> missing email.csv (8 files)
Outputs: output/enriched/<ICP> - <persona> enriched.csv (8 files)
         output/enriched/_prospeo_run_log.csv

Resumable: if an enriched file already has a row for a person (matched by
LinkedIn URL), it is left as-is and not re-queried.
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
IN_DIR = ROOT / "output" / "missing_emails"
OUT_DIR = ROOT / "output" / "enriched"
LOG_PATH = OUT_DIR / "_prospeo_run_log.csv"

API_URL = "https://api.prospeo.io/enrich-person"
API_KEY = os.environ.get("PROSPEO_API_KEY")
if not API_KEY:
    sys.exit("Set PROSPEO_API_KEY env var before running.")

REQUEST_INTERVAL_SEC = 0.5  # ~2 req/sec
TIMEOUT_SEC = 30
MAX_RETRIES = 3

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]


def call_prospeo(linkedin_url: str, full_name: str, website: str) -> dict:
    """Return a dict with: status, email, email_status, http_code, raw_error."""
    payload = {
        "only_verified_email": True,
        "enrich_mobile": False,
        "data": {},
    }
    if linkedin_url:
        payload["data"]["linkedin_url"] = linkedin_url
    elif full_name and website:
        parts = full_name.strip().split(None, 1)
        payload["data"]["first_name"] = parts[0]
        if len(parts) > 1:
            payload["data"]["last_name"] = parts[1]
        payload["data"]["company_website"] = website
    else:
        return {"status": "skipped_no_input", "email": "", "email_status": "",
                "http_code": 0, "raw_error": "no usable input"}

    headers = {"X-KEY": API_KEY, "Content-Type": "application/json"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                return {"status": "network_error", "email": "", "email_status": "",
                        "http_code": 0, "raw_error": str(e)[:200]}
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 429:
            time.sleep(5 * attempt)
            continue

        try:
            body = r.json()
        except ValueError:
            return {"status": "bad_json", "email": "", "email_status": "",
                    "http_code": r.status_code, "raw_error": r.text[:200]}

        if r.status_code != 200 or body.get("error"):
            return {"status": "api_error", "email": "", "email_status": "",
                    "http_code": r.status_code, "raw_error": json.dumps(body)[:300]}

        person = body.get("response", body).get("person") or body.get("person") or {}
        email_obj = person.get("email") or {}
        email = email_obj.get("email") if email_obj.get("revealed") else ""
        email_status = email_obj.get("status") or ""
        if email:
            return {"status": "found", "email": email, "email_status": email_status,
                    "http_code": 200, "raw_error": ""}
        return {"status": "no_email", "email": "", "email_status": email_status,
                "http_code": 200, "raw_error": ""}

    return {"status": "rate_limited", "email": "", "email_status": "",
            "http_code": 429, "raw_error": "exhausted retries"}


def load_existing(out_path: Path, persona: str) -> dict[str, dict]:
    """Map LinkedIn URL -> row of already-enriched data, for resume."""
    if not out_path.exists():
        return {}
    df = pd.read_csv(out_path, dtype=str, keep_default_na=False)
    key_col = f"{persona}_linkedin"
    if key_col not in df.columns or "prospeo_status" not in df.columns:
        return {}
    enriched = df[df["prospeo_status"] != ""]
    return {row[key_col]: row.to_dict() for _, row in enriched.iterrows() if row[key_col]}


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True, parents=True)
    log_rows: list[dict] = []
    if LOG_PATH.exists():
        log_rows = pd.read_csv(LOG_PATH, dtype=str, keep_default_na=False).to_dict("records")

    grand_credits = 0
    grand_calls = 0

    for icp in ICPS:
        for persona in PERSONAS:
            in_path = IN_DIR / f"{icp} - {persona} missing email.csv"
            out_path = OUT_DIR / f"{icp} - {persona} enriched.csv"
            if not in_path.exists():
                print(f"[skip] {in_path}")
                continue

            df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
            if df.empty:
                df.to_csv(out_path, index=False)
                continue

            for col in ("prospeo_status", "prospeo_email", "prospeo_email_status"):
                if col not in df.columns:
                    df[col] = ""

            existing = load_existing(out_path, persona)
            li_col = f"{persona}_linkedin"
            fn_col = f"{persona}_first_name"
            ln_col = f"{persona}_last_name"

            calls_this_file = 0
            credits_this_file = 0
            print(f"\n[{icp} / {persona}] {len(df)} rows")

            for i, row in df.iterrows():
                li = row.get(li_col, "").strip()
                if li and li in existing:
                    prev = existing[li]
                    df.at[i, "prospeo_status"] = prev.get("prospeo_status", "")
                    df.at[i, "prospeo_email"] = prev.get("prospeo_email", "")
                    df.at[i, "prospeo_email_status"] = prev.get("prospeo_email_status", "")
                    continue

                full_name = f"{row.get(fn_col, '')} {row.get(ln_col, '')}".strip()
                website = row.get("Website", "").strip()

                result = call_prospeo(li, full_name, website)
                df.at[i, "prospeo_status"] = result["status"]
                df.at[i, "prospeo_email"] = result["email"]
                df.at[i, "prospeo_email_status"] = result["email_status"]

                calls_this_file += 1
                if result["status"] == "found":
                    credits_this_file += 1

                log_rows.append({
                    "icp": icp, "persona": persona,
                    "company": row.get("Company Name", ""),
                    "linkedin_url": li,
                    "status": result["status"],
                    "email": result["email"],
                    "email_status": result["email_status"],
                    "http_code": result["http_code"],
                    "raw_error": result["raw_error"],
                })

                if calls_this_file % 25 == 0:
                    print(f"  ...{calls_this_file} calls, {credits_this_file} found")
                    df.to_csv(out_path, index=False)
                    pd.DataFrame(log_rows).to_csv(LOG_PATH, index=False)

                time.sleep(REQUEST_INTERVAL_SEC)

            df.to_csv(out_path, index=False)
            pd.DataFrame(log_rows).to_csv(LOG_PATH, index=False)
            print(f"  done: {calls_this_file} calls, ~{credits_this_file} credits charged")
            grand_calls += calls_this_file
            grand_credits += credits_this_file

    print(f"\n=== TOTAL: {grand_calls} new calls, ~{grand_credits} credits charged ===")


if __name__ == "__main__":
    main()
