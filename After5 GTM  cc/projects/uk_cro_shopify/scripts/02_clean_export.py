"""
Normalise raw_clutch.json → flat CSV.
Output: projects/uk_cro_shopify/output/uk_cro_shopify_agencies.csv

Run: python projects/uk_cro_shopify/scripts/02_clean_export.py
"""

import json
import re
import sys
from pathlib import Path

import pandas as pd

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
INPUT_FILE  = PROJECT_DIR / "output" / "raw_clutch.json"
OUTPUT_FILE = PROJECT_DIR / "output" / "uk_cro_shopify_agencies.csv"


def clean_rating(val: str) -> str:
    m = re.search(r"[\d.]+", val)
    return m.group() if m else ""


def clean_reviews(val: str) -> str:
    m = re.search(r"\d+", val.replace(",", ""))
    return m.group() if m else ""


def clean_website(url: str) -> str:
    """Strip tracking params and normalise to bare domain where possible."""
    url = url.strip()
    # Clutch wraps outbound links — extract the real destination if embedded
    m = re.search(r"https?://(?!clutch\.co)[^\s\"'&?]+", url)
    if m:
        return m.group().rstrip("/")
    return url


def main():
    if not INPUT_FILE.exists():
        print(f"Input not found: {INPUT_FILE}")
        print("Run 01_scrape_clutch.py first.")
        sys.exit(1)

    with open(INPUT_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    print(f"Loaded {len(raw)} raw records from {INPUT_FILE.name}")

    rows = []
    for a in raw:
        rows.append({
            "name":             a.get("name", "").strip(),
            "clutch_url":       a.get("clutch_url", "").strip(),
            "website":          clean_website(a.get("website", "")),
            "tagline":          a.get("tagline", "").strip(),
            "rating":           clean_rating(a.get("rating", "")),
            "reviews_count":    clean_reviews(a.get("reviews_count", "")),
            "location":         a.get("location", "").strip(),
            "min_project_size": a.get("min_project_size", "").strip(),
            "hourly_rate":      a.get("hourly_rate", "").strip(),
            "employees":        a.get("employees", "").strip(),
            "founded":          a.get("founded", "").strip(),
            "services":         ", ".join(a.get("services", [])),
        })

    df = pd.DataFrame(rows)

    # drop rows with no name
    before = len(df)
    df = df[df["name"].str.strip().astype(bool)].copy()
    print(f"Dropped {before - len(df)} unnamed rows")

    # deduplicate on clutch_url, then name
    df = df.drop_duplicates(subset=["clutch_url"], keep="first")
    df = df.drop_duplicates(subset=["name"], keep="first")

    # sort by reviews desc (most-reviewed agencies first), then name
    df["_reviews_int"] = pd.to_numeric(df["reviews_count"], errors="coerce").fillna(0)
    df = df.sort_values("_reviews_int", ascending=False).drop(columns=["_reviews_int"])
    df = df.reset_index(drop=True)

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8")

    print(f"\n{len(df)} agencies exported to {OUTPUT_FILE.name}")
    print(f"Columns: {list(df.columns)}")
    print("\nTop 5 by reviews:")
    print(df[["name", "rating", "reviews_count", "location"]].head())


if __name__ == "__main__":
    main()
