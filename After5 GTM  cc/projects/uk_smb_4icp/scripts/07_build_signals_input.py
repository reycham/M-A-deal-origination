"""Build the deduped per-company input for the signals enrichment layer.

Reads all 8 final_v2 CSVs, keeps only rows with deliverable email
(final_email_certainty in ultra_sure/very_sure/probable), dedups by normalised
LinkedIn URL, derives a clean domain (Website preferred, else email domain),
and writes output/signals/companies_to_enrich.csv.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FINAL_V2 = PROJECT_ROOT / "output" / "final_v2"
OUT_DIR = PROJECT_ROOT / "output" / "signals"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]
DELIVERABLE = {"ultra_sure", "very_sure", "probable"}

GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "mail.com",
    "btinternet.com", "sky.com", "ntlworld.com", "virginmedia.com",
}


def norm_li(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def domain_from_url(u: str) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = u.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0]
    return s.strip()


def domain_from_email(e: str) -> str:
    if not isinstance(e, str) or "@" not in e:
        return ""
    d = e.strip().lower().rsplit("@", 1)[1]
    if d in GENERIC_EMAIL_DOMAINS:
        return ""
    return d


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for icp in ICPS:
        for persona in PERSONAS:
            path = FINAL_V2 / f"{icp} - {persona} final.csv"
            if not path.exists():
                print(f"[skip] missing {path.name}")
                continue
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            mask = df["final_email_certainty"].isin(DELIVERABLE)
            sub = df[mask]
            print(f"[{icp} / {persona}] {len(df)} rows, {len(sub)} deliverable")
            for _, r in sub.iterrows():
                rows.append({
                    "icp": icp,
                    "persona": persona,
                    "linkedin_norm": norm_li(r.get("LinkedIn", "")),
                    "linkedin": r.get("LinkedIn", ""),
                    "company_name": r.get("Company Name", ""),
                    "website": r.get("Website", ""),
                    "sample_email": r.get("final_email", ""),
                })

    df = pd.DataFrame(rows)
    print(f"\nTotal deliverable rows across files: {len(df)}")

    df["domain"] = df["website"].map(domain_from_url)
    no_web = df["domain"] == ""
    df.loc[no_web, "domain"] = df.loc[no_web, "sample_email"].map(domain_from_email)

    # Dedup by normalised LinkedIn (keep first; persona/icp of first occurrence wins)
    before = len(df)
    df = df.drop_duplicates("linkedin_norm", keep="first").reset_index(drop=True)
    print(f"Unique companies after LinkedIn dedup: {len(df)} (removed {before - len(df)} duplicates)")

    with_dom = (df["domain"] != "").sum()
    print(f"With usable domain: {with_dom}  | without: {len(df) - with_dom}")

    out = OUT_DIR / "companies_to_enrich.csv"
    df[["linkedin_norm", "linkedin", "company_name", "website", "domain", "icp", "persona", "sample_email"]].to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
