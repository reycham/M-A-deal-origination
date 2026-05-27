"""Upload candidate emails to MillionVerifier, poll until done, download
results, then pick the best deliverable email per person and merge back
into the enriched CSVs.

Pattern priority (highest first): first.last > flast > first.l > firstlast > firstl > first
Status priority for what counts as "deliverable": ok > catch_all (treat as risky-but-usable)
"""
from __future__ import annotations

import os
import sys
import time
import io
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
PATTERNS_DIR = ROOT / "output" / "patterns"
ENRICHED_DIR = ROOT / "output" / "enriched"
VERIFIED_DIR = ROOT / "output" / "verified"
VERIFIED_DIR.mkdir(exist_ok=True, parents=True)

API_KEY = os.environ.get("MV_API_KEY")
if not API_KEY:
    sys.exit("Set MV_API_KEY env var.")

UPLOAD_URL = "https://bulkapi.millionverifier.com/bulkapi/v2/upload"
INFO_URL = "https://bulkapi.millionverifier.com/bulkapi/v2/fileinfo"
DOWNLOAD_URL = "https://bulkapi.millionverifier.com/bulkapi/v2/download"
LIST_URL = "https://bulkapi.millionverifier.com/bulkapi/v2/filelist"

CANDIDATES_FILE = PATTERNS_DIR / "candidates_emails_only.txt"
CANDIDATES_LONG = PATTERNS_DIR / "candidates_long.csv"

PATTERN_PRIORITY = ["first.last", "flast", "first.l", "firstlast", "firstl", "first"]
DELIVERABLE_STATUSES = {"ok"}                  # high confidence
ACCEPTABLE_STATUSES = {"ok", "catch_all"}      # usable but catch_all is risky


def upload_file() -> int:
    print(f"Uploading {CANDIDATES_FILE.name} to MillionVerifier...")
    with open(CANDIDATES_FILE, "rb") as f:
        files = {"file_contents": (CANDIDATES_FILE.name, f, "text/plain")}
        r = requests.post(UPLOAD_URL, params={"key": API_KEY}, files=files, timeout=120)
    r.raise_for_status()
    body = r.json()
    print(f"  Response: {body}")
    fid = body.get("file_id") or body.get("id")
    if not fid:
        sys.exit(f"No file_id in response: {body}")
    return int(fid)


def poll(file_id: int) -> dict:
    print(f"\nPolling job {file_id}...")
    while True:
        r = requests.get(INFO_URL, params={"key": API_KEY, "file_id": file_id}, timeout=60)
        r.raise_for_status()
        info = r.json()
        status = info.get("status", "?")
        pct = info.get("percent", 0)
        print(f"  status={status} percent={pct} verified={info.get('verified', '?')}/{info.get('total_rows', '?')}")
        if status in ("finished", "completed", "done"):
            return info
        if status in ("error", "failed"):
            sys.exit(f"Verification failed: {info}")
        time.sleep(15)


def download(file_id: int) -> pd.DataFrame:
    print(f"\nDownloading results for job {file_id}...")
    r = requests.get(DOWNLOAD_URL, params={"key": API_KEY, "file_id": file_id}, timeout=300)
    r.raise_for_status()
    raw = r.text
    raw_path = VERIFIED_DIR / f"_mv_raw_{file_id}.csv"
    raw_path.write_text(raw, encoding="utf-8")
    print(f"  Saved raw response to {raw_path}")
    df = pd.read_csv(io.StringIO(raw), dtype=str, keep_default_na=False)
    print(f"  Result columns: {list(df.columns)}")
    print(f"  Rows: {len(df)}")
    return df


def normalize_results(df: pd.DataFrame) -> pd.DataFrame:
    # MillionVerifier columns vary; try common ones
    email_col = next((c for c in df.columns if c.lower() in ("email", "address")), df.columns[0])
    status_col = next((c for c in df.columns if c.lower() in ("result", "status", "quality")), None)
    if status_col is None:
        sys.exit(f"Couldn't find status column. Columns: {list(df.columns)}")
    out = df[[email_col, status_col]].copy()
    out.columns = ["email", "result"]
    out["email"] = out["email"].str.strip().str.lower()
    out["result"] = out["result"].str.strip().str.lower()
    return out


def pick_best_per_person(candidates: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    cands = candidates.merge(results, left_on="candidate_email", right_on="email", how="left")
    cands["result"] = cands["result"].fillna("missing")
    pri = {p: i for i, p in enumerate(PATTERN_PRIORITY)}
    cands["_pat_pri"] = cands["pattern"].map(pri).fillna(99).astype(int)
    # Tier each result: 0=ok, 1=catch_all, 9=anything else
    cands["_res_tier"] = cands["result"].map(lambda r: 0 if r == "ok" else (1 if r == "catch_all" else 9))
    cands = cands.sort_values(["linkedin_url", "_res_tier", "_pat_pri"], kind="stable")
    best = cands.drop_duplicates("linkedin_url", keep="first").copy()

    # If best is a 9-tier (no ok/catch_all), nullify the email to make missing-status explicit
    best["picked_email"] = best.apply(
        lambda r: r["candidate_email"] if r["_res_tier"] < 9 else "", axis=1
    )
    best["picked_status"] = best.apply(
        lambda r: r["result"] if r["_res_tier"] < 9 else "no_deliverable", axis=1
    )
    return best[["icp", "persona", "linkedin_url", "company", "first_name", "last_name",
                 "domain", "picked_email", "picked_status", "pattern"]]


def merge_into_enriched(best: pd.DataFrame) -> None:
    for (icp, persona), grp in best.groupby(["icp", "persona"]):
        path = ENRICHED_DIR / f"{icp} - {persona} enriched.csv"
        if not path.exists():
            print(f"  [skip] {path}")
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        for col in ("mv_email", "mv_status", "mv_pattern"):
            if col not in df.columns:
                df[col] = ""
        li_col = f"{persona}_linkedin"
        for _, r in grp.iterrows():
            mask = df[li_col] == r["linkedin_url"]
            if mask.any():
                df.loc[mask, "mv_email"] = r["picked_email"]
                df.loc[mask, "mv_status"] = r["picked_status"]
                df.loc[mask, "mv_pattern"] = r["pattern"] if r["picked_email"] else ""
        out_path = VERIFIED_DIR / f"{icp} - {persona} verified.csv"
        df.to_csv(out_path, index=False)
        print(f"  Wrote {out_path} ({mask.shape[0]} rows)")


def main() -> None:
    file_id = upload_file()
    poll(file_id)
    raw = download(file_id)
    results = normalize_results(raw)
    print("\nResult-status distribution:")
    print(results["result"].value_counts().to_string())

    cands = pd.read_csv(CANDIDATES_LONG, dtype=str, keep_default_na=False)
    cands["candidate_email"] = cands["candidate_email"].str.lower()
    best = pick_best_per_person(cands, results)
    best_path = VERIFIED_DIR / "_best_per_person.csv"
    best.to_csv(best_path, index=False)
    print(f"\nBest-per-person saved to {best_path}")
    print(f"\nPicked-status distribution (1 row per person):")
    print(best["picked_status"].value_counts().to_string())

    # Yield numbers
    have_email = (best["picked_email"] != "").sum()
    print(f"\nPeople with a deliverable (ok/catch_all) email: {have_email}/{len(best)} ({100*have_email/len(best):.1f}%)")

    print("\nMerging into enriched CSVs...")
    merge_into_enriched(best)
    print("\nDone.")


if __name__ == "__main__":
    main()
