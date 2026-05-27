"""Merge MillionVerifier results into the enriched CSVs.

Picks the best deliverable email per person:
  result tier: ok < catch_all < everything else
  pattern priority: first.last > flast > first.l > firstlast > firstl > first
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
PATTERNS_DIR = ROOT / "output" / "patterns"
ENRICHED_DIR = ROOT / "output" / "enriched"
VERIFIED_DIR = ROOT / "output" / "verified"
VERIFIED_DIR.mkdir(exist_ok=True, parents=True)

MV_REPORT = ROOT / "candidates_emails_only_FULL_REPORT_MILLIONVERIFIER.COM.csv"
CANDIDATES_LONG = PATTERNS_DIR / "candidates_long.csv"

PATTERN_PRIORITY = ["first.last", "flast", "first.l", "firstlast", "firstl", "first"]


def main() -> None:
    mv = pd.read_csv(MV_REPORT, dtype=str, keep_default_na=False)
    mv["email"] = mv["email"].str.strip().str.lower()
    mv["result"] = mv["result"].str.strip().str.lower()

    print("Verifier result distribution:")
    print(mv["result"].value_counts().to_string())

    cands = pd.read_csv(CANDIDATES_LONG, dtype=str, keep_default_na=False)
    cands["candidate_email"] = cands["candidate_email"].str.lower()
    merged = cands.merge(mv[["email", "result"]], left_on="candidate_email", right_on="email", how="left")
    merged["result"] = merged["result"].fillna("missing")

    pri = {p: i for i, p in enumerate(PATTERN_PRIORITY)}
    merged["_pat_pri"] = merged["pattern"].map(pri).fillna(99).astype(int)
    merged["_res_tier"] = merged["result"].map(lambda r: 0 if r == "ok" else (1 if r == "catch_all" else 9))
    merged = merged.sort_values(["linkedin_url", "_res_tier", "_pat_pri"], kind="stable")

    best = merged.drop_duplicates("linkedin_url", keep="first").copy()
    best["picked_email"] = best.apply(
        lambda r: r["candidate_email"] if r["_res_tier"] < 9 else "", axis=1
    )
    best["picked_status"] = best.apply(
        lambda r: r["result"] if r["_res_tier"] < 9 else "no_deliverable", axis=1
    )
    best = best[["icp", "persona", "linkedin_url", "company", "first_name", "last_name",
                 "domain", "picked_email", "picked_status", "pattern"]]
    best_path = VERIFIED_DIR / "_best_per_person.csv"
    best.to_csv(best_path, index=False)

    print("\nBest-per-person status distribution:")
    print(best["picked_status"].value_counts().to_string())

    have_email = (best["picked_email"] != "").sum()
    print(f"\nPeople with a deliverable email: {have_email}/{len(best)} "
          f"({100*have_email/len(best):.1f}%)")

    print("\nBy ICP / persona / status:")
    print(best.groupby(["icp", "persona", "picked_status"]).size().unstack(fill_value=0).to_string())

    # Merge back into enriched files
    print("\nWriting verified CSVs...")
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
        print(f"  Wrote {out_path}")


if __name__ == "__main__":
    main()
