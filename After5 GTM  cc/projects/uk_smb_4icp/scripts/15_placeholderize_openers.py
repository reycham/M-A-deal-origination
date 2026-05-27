"""Replace the company token in every opener with the {{company_short}} placeholder.

Each opener follows the pattern: "Came across <token> today ...". We strip
<token> and replace it with the literal string `{{company_short}}` so Smartlead
can populate it from the company_short column at send time.

Reads:  output/openers/openers.csv
Writes: output/openers/openers.csv (overwrites the `opener` column in place)

After this, re-run `13_generate_opener.py --merge-only` to push placeholdered
openers into output/final_v6/.

Flags:
  --dry-run   show before/after for first 20 rows + counts, don't write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENERS_PATH = PROJECT_ROOT / "output" / "openers" / "openers.csv"

OUT_COLS = [
    "linkedin_norm", "company_name", "company_short",
    "branch", "opener", "status", "error",
]

PLACEHOLDER = "{{company_short}}"

# "Came across <anything non-greedy> today" — case-insensitive on the leading phrase
# but preserve the rest of the line as-is.
PATTERN = re.compile(r"^(Came\s+across)\s+(.+?)\s+(today)\b", re.IGNORECASE)


def patch(opener: str) -> tuple[str, bool]:
    if not isinstance(opener, str) or not opener.strip():
        return opener, False
    m = PATTERN.match(opener)
    if not m:
        return opener, False
    new = PATTERN.sub(rf"\1 {PLACEHOLDER} \3", opener, count=1)
    return new, True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not OPENERS_PATH.exists():
        sys.exit(f"Missing {OPENERS_PATH}. Run script 13 first.")

    df = pd.read_csv(OPENERS_PATH, dtype=str, keep_default_na=False)
    patched = 0
    skipped = 0
    examples: list[tuple[str, str]] = []

    for i, opener in enumerate(df["opener"]):
        new, ok = patch(opener)
        if ok:
            patched += 1
            if len(examples) < 20:
                examples.append((opener, new))
            df.at[i, "opener"] = new
        else:
            if opener.strip():
                skipped += 1

    print(f"Total rows: {len(df)}")
    print(f"Patched:    {patched}")
    print(f"Skipped (didn't match pattern, has text): {skipped}")
    print(f"Empty:      {(df['opener'].str.strip() == '').sum()}")
    print()
    print("First 20 patched (before -> after):")
    for before, after in examples:
        print(f"  - {before}")
        print(f"    {after}")

    if skipped:
        print("\nSkipped examples:")
        for op in df["opener"][~df["opener"].apply(lambda s: bool(PATTERN.match(s)) if s.strip() else False)].head(5):
            if op.strip():
                print(f"  {op}")

    if args.dry_run:
        print("\nDry run — not written.")
        return

    df[OUT_COLS].to_csv(OPENERS_PATH, index=False)
    print(f"\nWrote {OPENERS_PATH}")
    print("Next: python projects/uk_smb_4icp/scripts/13_generate_opener.py --merge-only")


if __name__ == "__main__":
    main()
