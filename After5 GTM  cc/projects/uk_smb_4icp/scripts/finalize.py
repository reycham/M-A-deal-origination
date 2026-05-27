"""Consolidate Apollo + Prospeo + MillionVerifier emails into one final
column per matched contact, then split into:
  - output/final/<ICP> - <persona> final.csv (everyone, best email picked)
  - output/final/<ICP> - <persona> still no email.csv (no email after all 3 sources)

Email priority: apollo > prospeo (verified) > mv (ok) > mv (catch_all)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
VERIFIED_DIR = ROOT / "output" / "verified"
ENRICHED_DIR = ROOT / "output" / "enriched"
MATCHED_DIR = ROOT / "output"
FINAL_DIR = ROOT / "output" / "final"
FINAL_DIR.mkdir(exist_ok=True, parents=True)

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]


def best_email(row: pd.Series, persona: str) -> tuple[str, str]:
    """Return (email, source) with priority: apollo > prospeo > mv ok > mv catch_all."""
    apollo = (row.get(f"{persona}_email") or "").strip()
    if apollo:
        return apollo, "apollo"
    prospeo = (row.get("prospeo_email") or "").strip()
    if prospeo:
        return prospeo, "prospeo_verified"
    mv = (row.get("mv_email") or "").strip()
    mv_status = (row.get("mv_status") or "").strip().lower()
    if mv and mv_status == "ok":
        return mv, "mv_ok"
    if mv and mv_status == "catch_all":
        return mv, "mv_catch_all"
    return "", "none"


def main() -> None:
    summary = []
    for icp in ICPS:
        for persona in PERSONAS:
            # Prefer the verified file (has Apollo + Prospeo + MV columns); fall back to enriched, then matched
            verified = VERIFIED_DIR / f"{icp} - {persona} verified.csv"
            enriched = ENRICHED_DIR / f"{icp} - {persona} enriched.csv"
            matched = MATCHED_DIR / f"{icp} - {persona}s.csv" if persona == "founder" else MATCHED_DIR / f"{icp} - sales.csv"

            df = pd.read_csv(verified, dtype=str, keep_default_na=False)

            # Reload from matched to get every company row (verified file only has missing-email subset)
            base = pd.read_csv(matched, dtype=str, keep_default_na=False)
            li_col = f"{persona}_linkedin"

            # Merge MV/Prospeo columns from verified file
            for col in ("prospeo_email", "prospeo_status", "mv_email", "mv_status", "mv_pattern"):
                if col not in base.columns:
                    base[col] = ""
            patch = df[df[li_col].astype(str).str.strip() != ""][[li_col, "prospeo_email", "prospeo_status", "mv_email", "mv_status", "mv_pattern"]]
            for _, r in patch.iterrows():
                mask = base[li_col] == r[li_col]
                if mask.any():
                    for c in ("prospeo_email", "prospeo_status", "mv_email", "mv_status", "mv_pattern"):
                        base.loc[mask, c] = r[c]

            # Compute final email
            best = base.apply(lambda r: best_email(r, persona), axis=1)
            base["final_email"] = [b[0] for b in best]
            base["final_email_source"] = [b[1] for b in best]

            final_path = FINAL_DIR / f"{icp} - {persona} final.csv"
            base.to_csv(final_path, index=False)

            # Matched-but-no-email subset
            matched_mask = base[f"{persona}_match_method"] != "none"
            no_email_mask = matched_mask & (base["final_email"] == "")
            still = base[no_email_mask]
            still_path = FINAL_DIR / f"{icp} - {persona} still no email.csv"
            still.to_csv(still_path, index=False)

            src_counts = base[matched_mask]["final_email_source"].value_counts().to_dict()
            summary.append({
                "icp": icp, "persona": persona,
                "total_companies": len(base),
                "matched": int(matched_mask.sum()),
                "with_email": int(matched_mask.sum() - no_email_mask.sum()),
                "still_no_email": int(no_email_mask.sum()),
                "from_apollo": src_counts.get("apollo", 0),
                "from_prospeo": src_counts.get("prospeo_verified", 0),
                "from_mv_ok": src_counts.get("mv_ok", 0),
                "from_mv_catch_all": src_counts.get("mv_catch_all", 0),
            })

    summ = pd.DataFrame(summary)
    summ.to_csv(FINAL_DIR / "_final_summary.csv", index=False)
    print(summ.to_string(index=False))
    print()
    totals = summ[["matched", "with_email", "still_no_email", "from_apollo", "from_prospeo", "from_mv_ok", "from_mv_catch_all"]].sum()
    print("TOTALS:")
    print(totals.to_string())
    print(f"\nOverall coverage: {totals['with_email']}/{totals['matched']} = {100*totals['with_email']/totals['matched']:.1f}%")


if __name__ == "__main__":
    main()
