"""Merge Adyntel + SimilarWeb signals into final_v2 -> final_v3.

For each of the 8 final_v2 files, append the new signals columns (Adyntel ad
counts joined on LinkedIn, SimilarWeb traffic joined on derived domain).
Same company appearing in both founder + sales files gets the same signals
values (since signals are per-company).

Outputs:
  output/final_v3/<ICP> - <persona> with signals.csv
  output/final_v3/_summary.csv
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FINAL_V2 = PROJECT_ROOT / "output" / "final_v2"
SIGNALS = PROJECT_ROOT / "output" / "signals"
OUT_DIR = PROJECT_ROOT / "output" / "final_v3"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]
DELIVERABLE = {"ultra_sure", "very_sure", "probable"}

GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "mail.com",
    "btinternet.com", "sky.com", "ntlworld.com", "virginmedia.com",
}

ADYNTEL_COLS = [
    "meta_ads_count", "adyntel_meta_status",
    "google_ads_count", "adyntel_google_status",
]
SW_COLS = [
    "sw_total_visits", "sw_bounce_rate", "sw_pages_per_visit",
    "sw_time_on_site", "sw_global_rank", "sw_category", "sw_top_country",
    "sw_status",
]


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

    adyntel_path = SIGNALS / "adyntel_results.csv"
    sw_path = SIGNALS / "similarweb_results.csv"

    if adyntel_path.exists():
        adyntel = pd.read_csv(adyntel_path, dtype=str, keep_default_na=False)
        adyntel = adyntel[["linkedin_norm"] + ADYNTEL_COLS].drop_duplicates("linkedin_norm", keep="first")
        print(f"Loaded {len(adyntel)} Adyntel rows")
    else:
        print("[warn] adyntel_results.csv missing — Adyntel columns will be blank")
        adyntel = pd.DataFrame(columns=["linkedin_norm"] + ADYNTEL_COLS)

    if sw_path.exists():
        sw = pd.read_csv(sw_path, dtype=str, keep_default_na=False)
        sw = sw[["domain"] + SW_COLS].drop_duplicates("domain", keep="first")
        print(f"Loaded {len(sw)} SimilarWeb rows")
    else:
        print("[warn] similarweb_results.csv missing — SimilarWeb columns will be blank")
        sw = pd.DataFrame(columns=["domain"] + SW_COLS)

    summary = []

    for icp in ICPS:
        for persona in PERSONAS:
            in_path = FINAL_V2 / f"{icp} - {persona} final.csv"
            if not in_path.exists():
                print(f"[skip] {in_path.name}")
                continue
            df = pd.read_csv(in_path, dtype=str, keep_default_na=False)

            df["_li_key"] = df["LinkedIn"].map(norm_li)
            df["_dom_key"] = df["Website"].map(domain_from_url)
            no_dom = df["_dom_key"] == ""
            df.loc[no_dom, "_dom_key"] = df.loc[no_dom, "final_email"].map(domain_from_email)

            n_in = len(df)
            df = df.merge(adyntel, left_on="_li_key", right_on="linkedin_norm", how="left")
            df = df.merge(sw, left_on="_dom_key", right_on="domain", how="left", suffixes=("", "_sw"))

            # Drop merge-key helpers and duplicated keys from joins
            df = df.drop(columns=["_li_key", "_dom_key", "linkedin_norm", "domain"], errors="ignore")
            assert len(df) == n_in, f"row-count drift on {in_path.name}: {n_in} -> {len(df)}"

            # Status enums for non-deliverable / unmatched rows
            deliverable_mask = df["final_email_certainty"].isin(DELIVERABLE)
            for col in ["adyntel_meta_status", "adyntel_google_status"]:
                df[col] = df[col].fillna("")
                df.loc[~deliverable_mask & (df[col] == ""), col] = "skipped_non_deliverable"
            df["sw_status"] = df["sw_status"].fillna("")
            df.loc[~deliverable_mask & (df["sw_status"] == ""), "sw_status"] = "skipped_non_deliverable"
            df.loc[deliverable_mask & (df["sw_status"] == ""), "sw_status"] = "no_data"

            # Blanks for numeric columns left as empty strings (consistent with rest of file)
            for col in ADYNTEL_COLS + SW_COLS:
                df[col] = df[col].fillna("")

            out_path = OUT_DIR / f"{icp} - {persona} with signals.csv"
            df.to_csv(out_path, index=False)

            deliverable = int(deliverable_mask.sum())
            has_meta = int((deliverable_mask & (df["meta_ads_count"].astype(str).replace("", "0").astype(float) > 0)).sum())
            has_google = int((deliverable_mask & (df["google_ads_count"].astype(str).replace("", "0").astype(float) > 0)).sum())
            has_sw = int((deliverable_mask & (df["sw_status"] == "ok")).sum())
            summary.append({
                "icp": icp, "persona": persona, "rows": len(df),
                "deliverable": deliverable,
                "has_meta_ads": has_meta,
                "has_google_ads": has_google,
                "has_sw_data": has_sw,
            })
            print(f"[{icp} / {persona}] rows={len(df)}  deliverable={deliverable}  "
                  f"meta>0={has_meta}  google>0={has_google}  sw_ok={has_sw}")

    pd.DataFrame(summary).to_csv(OUT_DIR / "_summary.csv", index=False)
    print(f"\nWrote {OUT_DIR}/_summary.csv")


if __name__ == "__main__":
    main()
