"""Adyntel signals enrichment: per-company Meta + Google ads counts.

Reads output/signals/companies_to_enrich.csv (script 07 output).
Calls Adyntel /facebook and /google for each company domain.
Writes output/signals/adyntel_results.csv (resumable; keyed by linkedin_norm).
Logs every call to output/signals/adyntel_log.csv.

Auth note: Adyntel takes {email, api_key} in JSON body, not as a header.

Flags:
  --limit N    process only the first N companies (smoke test)
  --resume     skip companies already present in adyntel_results.csv (default on)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lib.config import get_key  # noqa: E402

SIGNALS_DIR = PROJECT_ROOT / "output" / "signals"
IN_PATH = SIGNALS_DIR / "companies_to_enrich.csv"
OUT_PATH = SIGNALS_DIR / "adyntel_results.csv"
LOG_PATH = SIGNALS_DIR / "adyntel_log.csv"

API_KEY = get_key("ADYNTEL_API_KEY")
EMAIL = get_key("ADYNTEL_EMAIL")

META_URL = "https://api.adyntel.com/facebook"
GOOGLE_URL = "https://api.adyntel.com/google"

TIMEOUT_SEC = 90  # Adyntel doc states 60s sync limit
MAX_RETRIES = 3
MAX_WORKERS = 8  # concurrent companies in flight (×2 endpoints each)
CHECKPOINT_EVERY = 25

OUT_COLS = [
    "linkedin_norm", "company_name", "domain", "icp",
    "meta_ads_count", "adyntel_meta_status", "meta_http_code",
    "google_ads_count", "adyntel_google_status", "google_http_code",
    "adyntel_raw_json",
]


def adyntel_call(url: str, domain: str) -> dict:
    """Return {status, count, http_code, raw, error}."""
    body = {"email": EMAIL, "api_key": API_KEY, "company_domain": domain}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=body, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                return {"status": "network_error", "count": "", "http_code": 0,
                        "raw": "", "error": str(e)[:200]}
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 429:
            time.sleep(5 * attempt)
            continue
        if r.status_code in (502, 503, 504):
            if attempt == MAX_RETRIES:
                return {"status": "api_error", "count": "", "http_code": r.status_code,
                        "raw": r.text[:300], "error": "5xx"}
            time.sleep(2 ** attempt)
            continue

        # Adyntel returns 204 No Content (empty body) when the domain has no ads
        if r.status_code == 204 or not r.text.strip():
            return {"status": "ok", "count": 0, "http_code": r.status_code,
                    "raw": "", "error": ""}

        try:
            payload = r.json()
        except ValueError:
            return {"status": "bad_json", "count": "", "http_code": r.status_code,
                    "raw": r.text[:300], "error": "non-json response"}

        if r.status_code != 200:
            return {"status": "api_error", "count": "", "http_code": r.status_code,
                    "raw": json.dumps(payload)[:500], "error": str(payload.get("error", ""))[:200]}

        # Meta uses number_of_ads, Google uses total_ad_count
        count = payload.get("number_of_ads")
        if count is None:
            count = payload.get("total_ad_count")
        if count is None:
            # No explicit count field — try to infer from results/ads array length
            if isinstance(payload.get("results"), list):
                count = len(payload["results"])
            elif isinstance(payload.get("ads"), list):
                count = len(payload["ads"])
            else:
                count = 0

        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0

        return {"status": "ok", "count": count, "http_code": 200,
                "raw": json.dumps(payload)[:5000], "error": ""}

    return {"status": "rate_limited", "count": "", "http_code": 429,
            "raw": "", "error": "exhausted retries"}


def load_existing() -> dict[str, dict]:
    if not OUT_PATH.exists():
        return {}
    df = pd.read_csv(OUT_PATH, dtype=str, keep_default_na=False)
    return {row["linkedin_norm"]: row.to_dict() for _, row in df.iterrows() if row.get("linkedin_norm")}


def enrich_one(row: dict) -> tuple[dict, list[dict]]:
    """Process one company → (out_row, [log_rows])."""
    li = row["linkedin_norm"]
    domain = (row.get("domain") or "").strip()
    company = row.get("company_name", "")
    icp = row.get("icp", "")

    if not domain:
        out = {
            "linkedin_norm": li, "company_name": company, "domain": "", "icp": icp,
            "meta_ads_count": "", "adyntel_meta_status": "skipped_no_domain", "meta_http_code": "",
            "google_ads_count": "", "adyntel_google_status": "skipped_no_domain", "google_http_code": "",
            "adyntel_raw_json": "",
        }
        return out, []

    meta = adyntel_call(META_URL, domain)
    google = adyntel_call(GOOGLE_URL, domain)

    out = {
        "linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
        "meta_ads_count": meta["count"], "adyntel_meta_status": meta["status"], "meta_http_code": meta["http_code"],
        "google_ads_count": google["count"], "adyntel_google_status": google["status"], "google_http_code": google["http_code"],
        "adyntel_raw_json": json.dumps({"meta": meta["raw"], "google": google["raw"]})[:9000],
    }
    logs = [
        {"linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
         "platform": "meta", "status": meta["status"], "count": meta["count"],
         "http_code": meta["http_code"], "error": meta["error"]},
        {"linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
         "platform": "google", "status": google["status"], "count": google["count"],
         "http_code": google["http_code"], "error": google["error"]},
    ]
    return out, logs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process only first N rows")
    ap.add_argument("--no-resume", action="store_true", help="re-query everything")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"concurrent companies (default {MAX_WORKERS})")
    args = ap.parse_args()

    if not IN_PATH.exists():
        sys.exit(f"Missing {IN_PATH}. Run 07_build_signals_input.py first.")

    df_in = pd.read_csv(IN_PATH, dtype=str, keep_default_na=False)
    if args.limit:
        df_in = df_in.head(args.limit)

    existing = {} if args.no_resume else load_existing()
    log_rows: list[dict] = []
    if LOG_PATH.exists():
        log_rows = pd.read_csv(LOG_PATH, dtype=str, keep_default_na=False).to_dict("records")

    out_rows: list[dict] = list(existing.values()) if not args.no_resume else []
    out_keys = {r["linkedin_norm"] for r in out_rows}

    todo_rows = [r for _, r in df_in.iterrows() if r["linkedin_norm"] not in out_keys]
    print(f"Input: {len(df_in)} companies. Already done: {len(existing)}. To process: {len(todo_rows)} "
          f"(×2 endpoints, {args.workers} workers)")

    if not todo_rows:
        return

    lock = threading.Lock()
    completed = 0
    started = time.time()

    def write_checkpoint() -> None:
        pd.DataFrame(out_rows)[OUT_COLS].to_csv(OUT_PATH, index=False)
        pd.DataFrame(log_rows).to_csv(LOG_PATH, index=False)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(enrich_one, dict(r)): r["linkedin_norm"] for r in todo_rows}
        for fut in as_completed(futures):
            try:
                out, logs = fut.result()
            except Exception as e:
                li = futures[fut]
                print(f"  [error] {li}: {e}")
                continue
            with lock:
                out_rows.append(out)
                log_rows.extend(logs)
                completed += 1
                if completed % CHECKPOINT_EVERY == 0:
                    elapsed = time.time() - started
                    rate = completed / elapsed
                    eta = (len(todo_rows) - completed) / rate if rate > 0 else 0
                    print(f"  ...{completed}/{len(todo_rows)}  "
                          f"({rate:.1f}/s, eta {eta/60:.1f} min)  "
                          f"last: {out['company_name']} meta={out['adyntel_meta_status']}/{out['meta_ads_count']} "
                          f"google={out['adyntel_google_status']}/{out['google_ads_count']}")
                    write_checkpoint()

    write_checkpoint()
    print(f"\nDone. {completed} companies processed in {(time.time()-started)/60:.1f} min. Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
