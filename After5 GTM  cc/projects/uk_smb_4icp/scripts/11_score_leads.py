"""Lead scoring on top of final_v3.

Reads each output/final_v3/<ICP> - <persona> with signals.csv, computes a
0-100 lead_score from a transparent weighted-sum rubric, and assigns a tier:
  Hot   (>=75) | Warm (50-74) | Cool (25-49) | Cold (<25)   for deliverable rows
  X                                                          for non-deliverable

Outputs: output/final_v4/<ICP> - <persona> scored.csv  (8 files)
         output/final_v4/_all_deliverable_ranked.csv    (master ranked CSV)
         output/final_v4/_summary.csv                   (per-ICP/persona tier counts)
         output/final_v4/_score_distribution.csv        (score histogram)

Tune `WEIGHTS` block at the top after seeing the first distribution.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IN_DIR = PROJECT_ROOT / "output" / "final_v3"
OUT_DIR = PROJECT_ROOT / "output" / "final_v4"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]
DELIVERABLE = {"ultra_sure", "very_sure", "probable"}

# ---------------------------------------------------------------------------
# WEIGHTS — tune here. Each component is bounded; max total = 100.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "email_certainty": {"ultra_sure": 10, "very_sure": 7, "probable": 4},
    "ads_meta_present": 10,
    "ads_google_present": 10,
    "ads_heavy_bonus": 5,           # if meta_count>50 OR google_count>100
    "ads_heavy_meta_threshold": 50,
    "ads_heavy_google_threshold": 100,
    "traffic_buckets": [             # (lo_inclusive, hi_exclusive, points)
        (0, 500, 0),
        (500, 2000, 8),
        (2000, 10000, 18),
        (10000, 50000, 25),
        (50000, 10**12, 30),
    ],
    "engagement_low_bounce_pts": 5,  # bounce < 50
    "engagement_pages_pts": 5,       # pages_per_visit > 2
    "geo_gb_pts": 5,
    "dm_senior_pts": 10,             # founder/CEO/MD title hit
    "dm_other_match_pts": 5,         # any founder match w/o senior title
    "revenue_band_pts": {            # exact-string match on Apollo's range buckets
        "500000-999999": 10,
        "1000000-4999999": 10,
        "5000000-9999999": 8,
        "10000000-24999999": 5,
        "25000000-49999999": 5,
        "50000000-99999999": 5,
        "100000-499999": 2,
        "0-9999": 2,
        "10000-49999": 2,
        "50000-99999": 2,
    },
}

TIER_BANDS = [  # (min_score, label)
    (75, "Hot"),
    (50, "Warm"),
    (25, "Cool"),
    (0, "Cold"),
]

SENIOR_TITLE_RE = re.compile(
    r"\b(founder|co[- ]?founder|owner|ceo|chief\s+executive|"
    r"managing\s+director|\bmd\b|president|proprietor|principal)\b",
    re.I,
)


def to_int(v) -> int:
    try:
        s = str(v).strip()
        if not s:
            return 0
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def to_float(v) -> float | None:
    try:
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def score_row(row: dict) -> int:
    score = 0

    # 1. Email certainty
    score += WEIGHTS["email_certainty"].get(row.get("final_email_certainty", ""), 0)

    # 2. Paid-ad activity
    meta = to_int(row.get("meta_ads_count"))
    google = to_int(row.get("google_ads_count"))
    if meta > 0:
        score += WEIGHTS["ads_meta_present"]
    if google > 0:
        score += WEIGHTS["ads_google_present"]
    if meta > WEIGHTS["ads_heavy_meta_threshold"] or google > WEIGHTS["ads_heavy_google_threshold"]:
        score += WEIGHTS["ads_heavy_bonus"]

    # 3. Traffic volume (bucketed)
    visits = to_int(row.get("sw_total_visits"))
    for lo, hi, pts in WEIGHTS["traffic_buckets"]:
        if lo <= visits < hi:
            score += pts
            break

    # 4. Engagement quality (only if we have SimilarWeb data)
    if row.get("sw_status") == "ok":
        bounce = to_float(row.get("sw_bounce_rate"))
        pages = to_float(row.get("sw_pages_per_visit"))
        if bounce is not None and bounce < 50:
            score += WEIGHTS["engagement_low_bounce_pts"]
        if pages is not None and pages > 2:
            score += WEIGHTS["engagement_pages_pts"]

    # 5. Geo
    if row.get("sw_top_country") == "GB":
        score += WEIGHTS["geo_gb_pts"]

    # 6. Decision-maker seniority
    title = (row.get("founder_title") or "").strip()
    match_method = (row.get("founder_match_method") or "").strip()
    if title and SENIOR_TITLE_RE.search(title):
        score += WEIGHTS["dm_senior_pts"]
    elif match_method and match_method != "none":
        score += WEIGHTS["dm_other_match_pts"]

    # 7. Revenue sweet spot
    rev = (row.get("Annual Revenue") or "").strip()
    score += WEIGHTS["revenue_band_pts"].get(rev, 0)

    return min(100, max(0, score))


def tier_for(score: int, deliverable: bool) -> str:
    if not deliverable:
        return "X"
    for threshold, label in TIER_BANDS:
        if score >= threshold:
            return label
    return "Cold"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict] = []
    master: list[pd.DataFrame] = []

    for icp in ICPS:
        for persona in PERSONAS:
            in_path = IN_DIR / f"{icp} - {persona} with signals.csv"
            if not in_path.exists():
                print(f"[skip] {in_path.name}")
                continue
            df = pd.read_csv(in_path, dtype=str, keep_default_na=False)

            # icp / persona aren't in the file — add them for the master CSV
            df["icp"] = icp
            df["persona"] = persona

            scores = [score_row(r) for r in df.to_dict("records")]
            df["lead_score"] = scores
            df["tier"] = [
                tier_for(s, c in DELIVERABLE)
                for s, c in zip(scores, df["final_email_certainty"])
            ]

            out_path = OUT_DIR / f"{icp} - {persona} scored.csv"
            df.to_csv(out_path, index=False)

            tier_counts = df["tier"].value_counts().to_dict()
            deliverable_mask = df["final_email_certainty"].isin(DELIVERABLE)
            mean_deliverable = df.loc[deliverable_mask, "lead_score"].mean() if deliverable_mask.any() else 0
            summary_rows.append({
                "icp": icp, "persona": persona,
                "rows": len(df),
                "Hot": tier_counts.get("Hot", 0),
                "Warm": tier_counts.get("Warm", 0),
                "Cool": tier_counts.get("Cool", 0),
                "Cold": tier_counts.get("Cold", 0),
                "X": tier_counts.get("X", 0),
                "mean_score_deliverable": round(mean_deliverable, 1),
            })
            print(f"[{icp} / {persona}]  rows={len(df)}  "
                  f"Hot={tier_counts.get('Hot',0)}  Warm={tier_counts.get('Warm',0)}  "
                  f"Cool={tier_counts.get('Cool',0)}  Cold={tier_counts.get('Cold',0)}  "
                  f"X={tier_counts.get('X',0)}  mean(deliv)={mean_deliverable:.1f}")

            master.append(df[deliverable_mask])

    # Master ranked CSV (deliverable only, sorted)
    KEY_COLS = [
        "tier", "lead_score", "Company Name", "icp", "persona", "final_email",
        "founder_title", "founder_first_name", "founder_last_name",
        "meta_ads_count", "google_ads_count",
        "sw_total_visits", "sw_top_country", "sw_category",
        "Headcount", "Annual Revenue", "LinkedIn", "Website",
    ]
    if master:
        ranked = pd.concat(master, ignore_index=True).sort_values("lead_score", ascending=False)
        present_cols = [c for c in KEY_COLS if c in ranked.columns]
        other_cols = [c for c in ranked.columns if c not in present_cols]
        ranked[present_cols + other_cols].to_csv(OUT_DIR / "_all_deliverable_ranked.csv", index=False)

    pd.DataFrame(summary_rows).to_csv(OUT_DIR / "_summary.csv", index=False)

    # Score distribution
    if master:
        all_scores = pd.concat([df[["lead_score", "tier"]] for df in master], ignore_index=True)
        dist = all_scores.groupby("lead_score").size().reset_index(name="count").sort_values("lead_score")
        dist.to_csv(OUT_DIR / "_score_distribution.csv", index=False)

    # Stdout summary
    sm = pd.DataFrame(summary_rows)
    print()
    print("=== Tier totals (deliverable) ===")
    print(sm[["Hot", "Warm", "Cool", "Cold", "X"]].sum().to_string())

    if master:
        print("\n=== Top 10 leads ===")
        top = pd.concat(master, ignore_index=True).nlargest(10, "lead_score")
        print(top[["tier", "lead_score", "Company Name", "icp", "founder_title",
                   "meta_ads_count", "google_ads_count", "sw_total_visits"]].to_string(index=False))

    print(f"\nWrote {OUT_DIR}/")


if __name__ == "__main__":
    main()
