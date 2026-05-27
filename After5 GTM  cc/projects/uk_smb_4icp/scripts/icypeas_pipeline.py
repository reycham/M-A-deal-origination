"""Icypeas pipeline: find missing emails, then verify all emails.

Phase 1: For each of the 582 still-no-email contacts, run /api/bulk-search
         with task=email-search using (first, last, domain).
Phase 2: Collect every email we have (Apollo + Prospeo + MV + new Icypeas
         finds) and run /api/bulk-search with task=email-verification.

Outputs:
  output/icypeas/phase1_finds.csv     - newly discovered emails per contact
  output/icypeas/phase2_verified.csv  - verification result for every email
  output/final_v2/<ICP> - <persona> final.csv  - unified final per persona
  output/final_v2/_summary.csv
"""
from __future__ import annotations

import os
import re
import sys
import time
import hmac
import hashlib
import datetime
import json
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
FINAL_DIR = ROOT / "output" / "final"
ICY_DIR = ROOT / "output" / "icypeas"
OUT_DIR = ROOT / "output" / "final_v2"
ICY_DIR.mkdir(exist_ok=True, parents=True)
OUT_DIR.mkdir(exist_ok=True, parents=True)

API_KEY = os.environ["ICY_API_KEY"]
API_SECRET = os.environ["ICY_API_SECRET"]
USER_ID = os.environ["ICY_USER_ID"]
BASE = "https://app.icypeas.com"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]


def now_iso() -> str:
    n = datetime.datetime.now(datetime.UTC)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def sign(method: str, endpoint: str) -> tuple[str, dict]:
    ts = now_iso()
    s = (method + endpoint + ts).lower()
    sig = hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha1).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"{API_KEY}:{sig}",
        "X-ROCK-TIMESTAMP": ts,
    }
    return ts, headers


def post(endpoint: str, body: dict, timeout: int = 60) -> dict:
    _, headers = sign("POST", endpoint)
    r = requests.post(BASE + endpoint, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{endpoint} -> HTTP {r.status_code}: {r.text[:400]}")
    body = r.json()
    if not body.get("success", True):
        raise RuntimeError(f"{endpoint} -> not success: {json.dumps(body)[:400]}")
    return body


def domain_from_url(u: str) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = u.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    return s.split("/", 1)[0].split("?", 1)[0]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — find emails for the still-missing contacts

def collect_missing_contacts() -> pd.DataFrame:
    rows = []
    for icp in ICPS:
        for persona in PERSONAS:
            p = FINAL_DIR / f"{icp} - {persona} still no email.csv"
            if not p.exists():
                continue
            df = pd.read_csv(p, dtype=str, keep_default_na=False)
            for _, r in df.iterrows():
                first = (r.get(f"{persona}_first_name") or "").strip()
                last = (r.get(f"{persona}_last_name") or "").strip()
                domain = domain_from_url(r.get("Website") or "")
                if not (first and last and domain):
                    continue
                rows.append({
                    "icp": icp, "persona": persona,
                    "company": r.get("Company Name", ""),
                    "linkedin_url": r.get(f"{persona}_linkedin", ""),
                    "first_name": first, "last_name": last, "domain": domain,
                })
    return pd.DataFrame(rows)


def launch_bulk(task: str, name: str, data: list[list[str]], external_ids: list[str]) -> str:
    body = {
        "user": USER_ID,
        "name": name,
        "task": task,
        "data": data,
        "custom": {"externalIds": external_ids},
    }
    print(f"  Submitting bulk: task={task} rows={len(data)}")
    resp = post("/api/bulk-search", body, timeout=120)
    file_id = resp.get("file") or resp.get("fileId") or resp.get("_id")
    if not file_id:
        raise RuntimeError(f"No file id in response: {resp}")
    print(f"  file_id = {file_id}")
    return file_id


def wait_for_bulk(file_id: str, label: str) -> dict:
    print(f"  Polling {label}...")
    while True:
        resp = post("/api/search-files/read", {"file": file_id}, timeout=30)
        files = resp.get("files", [])
        if not files:
            time.sleep(5)
            continue
        f = files[0]
        finished = f.get("finished")
        done = f.get("done", 0)
        total = f.get("total", 0)
        in_prog = f.get("in-progress", 0)
        found = f.get("found", 0)
        print(f"    done={done}/{total}  in_progress={in_prog}  found={found}  finished={finished}")
        if finished or (total and done + f.get("aborted", 0) + f.get("bad-input", 0) >= total and in_prog == 0):
            return f
        time.sleep(20)


def fetch_bulk_results(file_id: str) -> list[dict]:
    items: list[dict] = []
    last_sort = None
    while True:
        body: dict = {"mode": "bulk", "file": file_id, "limit": 100}
        if last_sort is not None:
            body["next"] = True
            body["sorts"] = [last_sort]
        time.sleep(2.1)  # rate limit: 30/min
        resp = post("/api/bulk-single-searchs/read", body, timeout=60)
        page = resp.get("items") or []
        if not page:
            break
        items.extend(page)
        sorts = resp.get("sorts") or []
        if sorts:
            last_sort = sorts[-1]
        print(f"    fetched {len(items)} so far...")
        if len(page) < 100:
            break
    return items


def _parse_common(item: dict) -> dict:
    """Shared parser: pulls email, certainty, status, external_id from an item."""
    results = item.get("results") or {}
    emails = results.get("emails") or []
    email = ""
    certainty = ""
    if emails and isinstance(emails[0], dict):
        email = emails[0].get("email", "") or ""
        certainty = emails[0].get("certainty", "") or ""
    user_data = item.get("userData") or {}
    return {
        "email": email.lower(),
        "certainty": certainty,
        "status": item.get("status", ""),
        "external_id": user_data.get("externalId", ""),
    }


def parse_email_search_item(item: dict) -> dict:
    return _parse_common(item)


def parse_verification_item(item: dict) -> dict:
    return _parse_common(item)


# ─────────────────────────────────────────────────────────────────────────────
# Main

def phase1_find() -> pd.DataFrame:
    print("\n=== Phase 1: Email-search for missing contacts ===")
    contacts = collect_missing_contacts()
    print(f"Eligible contacts (first+last+domain available): {len(contacts)}")
    if contacts.empty:
        return contacts

    data = [[r.first_name, r.last_name, r.domain] for r in contacts.itertuples()]
    ext_ids = [f"p1_{i}" for i in range(len(contacts))]

    file_id = launch_bulk("email-search", "GTM phase1 missing-email finder", data, ext_ids)
    (ICY_DIR / "phase1_file_id.txt").write_text(file_id)

    wait_for_bulk(file_id, "phase 1 email-search")
    items = fetch_bulk_results(file_id)
    print(f"  Total result items: {len(items)}")

    parsed = []
    for it in items:
        p = parse_email_search_item(it)
        parsed.append(p)
    res_df = pd.DataFrame(parsed)
    # Map external_id -> contact row
    res_df["_idx"] = res_df["external_id"].str.replace("p1_", "", regex=False).astype("Int64")
    contacts["_idx"] = range(len(contacts))
    merged = contacts.merge(res_df, on="_idx", how="left").drop(columns=["_idx"])
    merged.to_csv(ICY_DIR / "phase1_finds.csv", index=False)

    found_mask = merged["email"].fillna("").str.strip() != ""
    print(f"  New emails found: {found_mask.sum()}/{len(merged)}")
    return merged[found_mask].copy()


def collect_all_emails_for_verification(new_finds: pd.DataFrame) -> pd.DataFrame:
    """All unique emails to verify: existing finals + new icypeas finds."""
    rows = []
    for icp in ICPS:
        for persona in PERSONAS:
            p = FINAL_DIR / f"{icp} - {persona} final.csv"
            if not p.exists():
                continue
            df = pd.read_csv(p, dtype=str, keep_default_na=False)
            mask = (df.get("final_email", "").astype(str).str.strip() != "")
            for _, r in df[mask].iterrows():
                rows.append({
                    "icp": icp, "persona": persona,
                    "linkedin_url": r.get(f"{persona}_linkedin", ""),
                    "company": r.get("Company Name", ""),
                    "email": r.get("final_email", "").strip().lower(),
                    "source": r.get("final_email_source", ""),
                })
    if not new_finds.empty:
        for _, r in new_finds.iterrows():
            rows.append({
                "icp": r["icp"], "persona": r["persona"],
                "linkedin_url": r.get("linkedin_url", ""),
                "company": r.get("company", ""),
                "email": str(r.get("email", "")).strip().lower(),
                "source": "icypeas_found",
            })
    df = pd.DataFrame(rows)
    df = df[df["email"] != ""]
    return df


def phase2_verify(all_email_df: pd.DataFrame) -> pd.DataFrame:
    print("\n=== Phase 2: Verify every email ===")
    unique_emails = sorted(all_email_df["email"].unique().tolist())
    print(f"Unique emails to verify: {len(unique_emails)}")

    data = [[e] for e in unique_emails]
    ext_ids = [f"v_{i}" for i in range(len(unique_emails))]
    file_id = launch_bulk("email-verification", "GTM phase2 verify all", data, ext_ids)
    (ICY_DIR / "phase2_file_id.txt").write_text(file_id)

    wait_for_bulk(file_id, "phase 2 verification")
    items = fetch_bulk_results(file_id)
    print(f"  Total result items: {len(items)}")

    parsed = [parse_verification_item(it) for it in items]
    df = pd.DataFrame(parsed)
    df.to_csv(ICY_DIR / "phase2_verified.csv", index=False)
    print("  Result certainty distribution:")
    if "certainty" in df.columns:
        print(df["certainty"].value_counts().to_string())
    return df


def merge_final(all_email_df: pd.DataFrame, verify_df: pd.DataFrame) -> None:
    print("\n=== Merging into final_v2 ===")
    DELIVERABLE = {"very_sure", "ultra_sure", "probable"}
    BAD = {"undeliverable"}

    vmap = dict(zip(verify_df["email"].str.lower(), verify_df["certainty"].str.lower()))
    all_email_df = all_email_df.copy()
    all_email_df["icypeas_certainty"] = all_email_df["email"].map(lambda e: vmap.get(e, ""))
    all_email_df["deliverable"] = all_email_df["icypeas_certainty"].isin(DELIVERABLE)

    # For each contact (linkedin_url), pick best email by source/certainty
    SOURCE_RANK = {"apollo": 1, "prospeo_verified": 2, "icypeas_found": 3,
                   "mv_ok": 4, "mv_catch_all": 5}
    CERT_RANK = {"ultra_sure": 0, "very_sure": 0, "probable": 1, "": 5,
                 "not_found": 6, "undeliverable": 9}

    all_email_df["_src"] = all_email_df["source"].map(lambda s: SOURCE_RANK.get(s, 9))
    all_email_df["_cert"] = all_email_df["icypeas_certainty"].map(lambda c: CERT_RANK.get(c, 5))
    sorted_df = all_email_df.sort_values(["_cert", "_src"], kind="stable")
    best = sorted_df.drop_duplicates("linkedin_url", keep="first")

    # Build final per (icp, persona): rejoin to original final files for full company columns
    summary_rows = []
    for icp in ICPS:
        for persona in PERSONAS:
            base_path = FINAL_DIR / f"{icp} - {persona} final.csv"
            if not base_path.exists():
                continue
            df = pd.read_csv(base_path, dtype=str, keep_default_na=False)
            li_col = f"{persona}_linkedin"

            best_slice = best[(best["icp"] == icp) & (best["persona"] == persona)]
            li_to_email = dict(zip(best_slice["linkedin_url"], best_slice["email"]))
            li_to_src = dict(zip(best_slice["linkedin_url"], best_slice["source"]))
            li_to_cert = dict(zip(best_slice["linkedin_url"], best_slice["icypeas_certainty"]))

            # Preserve original columns; replace final_email/source with verified-best
            df["final_email"] = df[li_col].map(li_to_email).fillna("")
            df["final_email_source"] = df[li_col].map(li_to_src).fillna("none")
            df["final_email_certainty"] = df[li_col].map(li_to_cert).fillna("")

            out_path = OUT_DIR / f"{icp} - {persona} final.csv"
            df.to_csv(out_path, index=False)

            matched = (df[f"{persona}_match_method"] != "none").sum()
            with_email = (df["final_email"] != "").sum()
            deliverable = df["final_email_certainty"].isin(DELIVERABLE).sum()
            summary_rows.append({
                "icp": icp, "persona": persona,
                "matched": matched, "with_email": with_email,
                "deliverable_high_conf": deliverable,
            })
            print(f"  {icp}/{persona}: matched={matched} with_email={with_email} deliverable={deliverable}")

    summ = pd.DataFrame(summary_rows)
    summ.to_csv(OUT_DIR / "_summary.csv", index=False)
    print("\nSummary:")
    print(summ.to_string(index=False))
    t = summ[["matched", "with_email", "deliverable_high_conf"]].sum()
    print(f"\nTotals: matched={t['matched']}  with_email={t['with_email']}  "
          f"deliverable={t['deliverable_high_conf']} "
          f"({100*t['deliverable_high_conf']/t['matched']:.1f}%)")


def main() -> None:
    finds = phase1_find()
    all_emails = collect_all_emails_for_verification(finds)
    verify = phase2_verify(all_emails)
    merge_final(all_emails, verify)
    print("\nDone.")


if __name__ == "__main__":
    main()
