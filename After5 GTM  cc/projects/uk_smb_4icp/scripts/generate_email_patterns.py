"""For every NO_MATCH contact from the Prospeo run, generate plausible
email patterns from (first, last, company domain). Output a long-format
CSV ready for bulk upload to any verifier (MillionVerifier, Reoon,
ZeroBounce, NeverBounce, etc.).

One person -> 8 candidate emails. No API calls, no cost.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
ENRICHED_DIR = ROOT / "output" / "enriched"
OUT_DIR = ROOT / "output" / "patterns"
OUT_DIR.mkdir(exist_ok=True, parents=True)

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "mail.com",
    "btinternet.com", "sky.com", "ntlworld.com", "virginmedia.com",
}


def domain_from_url(u: str) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = u.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0]
    return s


def slug(name: str) -> str:
    """Lowercase ASCII; strip accents/punctuation/whitespace."""
    if not isinstance(name, str):
        return ""
    s = name.lower().strip()
    # Strip accents
    repl = str.maketrans("àáâãäåèéêëìíîïòóôõöùúûüçñ", "aaaaaaeeeeiiiiooooouuuucn")
    s = s.translate(repl)
    s = re.sub(r"[^a-z]", "", s)
    return s


def patterns(first: str, last: str, domain: str) -> list[tuple[str, str]]:
    f = slug(first)
    l = slug(last)
    if not (f and l and domain):
        return []
    fi = f[0]
    li = l[0]
    pats = [
        ("first.last",       f"{f}.{l}@{domain}"),
        ("firstlast",        f"{f}{l}@{domain}"),
        ("first",            f"{f}@{domain}"),
        ("flast",            f"{fi}{l}@{domain}"),
        ("first.l",          f"{f}.{li}@{domain}"),
        ("firstl",           f"{f}{li}@{domain}"),
    ]
    return pats


def main() -> None:
    log = pd.read_csv(ENRICHED_DIR / "_prospeo_run_log.csv", dtype=str, keep_default_na=False)
    no_match = log[log["status"] == "api_error"].copy()
    print(f"NO_MATCH contacts to pattern: {len(no_match)}")

    # Pull first/last/website from enriched files keyed on linkedin_url
    enrich_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def info_for(icp: str, persona: str, li: str) -> dict:
        key = (icp, persona)
        if key not in enrich_cache:
            enrich_cache[key] = pd.read_csv(
                ENRICHED_DIR / f"{icp} - {persona} enriched.csv",
                dtype=str, keep_default_na=False,
            )
        df = enrich_cache[key]
        m = df[df[f"{persona}_linkedin"] == li]
        if m.empty:
            return {}
        r = m.iloc[0]
        return {
            "first": r.get(f"{persona}_first_name", ""),
            "last": r.get(f"{persona}_last_name", ""),
            "website": r.get("Website", ""),
            "company": r.get("Company Name", ""),
        }

    rows = []
    skipped = 0
    domain_blocked = 0
    for _, src in no_match.iterrows():
        info = info_for(src["icp"], src["persona"], src["linkedin_url"])
        if not info:
            skipped += 1
            continue
        d = domain_from_url(info["website"])
        if not d:
            skipped += 1
            continue
        if d in GENERIC_DOMAINS:
            domain_blocked += 1
            continue
        for label, candidate in patterns(info["first"], info["last"], d):
            rows.append({
                "icp": src["icp"], "persona": src["persona"],
                "company": info["company"], "domain": d,
                "first_name": info["first"], "last_name": info["last"],
                "linkedin_url": src["linkedin_url"],
                "pattern": label, "candidate_email": candidate,
            })

    out_long = pd.DataFrame(rows)
    long_path = OUT_DIR / "candidates_long.csv"
    out_long.to_csv(long_path, index=False)

    # Also write a flat email-only file for easy bulk verifier upload
    flat_path = OUT_DIR / "candidates_emails_only.txt"
    flat_path.write_text("\n".join(out_long["candidate_email"].unique()) + "\n", encoding="utf-8")

    # Per-ICP summary
    summary = out_long.groupby(["icp", "persona"]).agg(
        people=("linkedin_url", "nunique"),
        candidates=("candidate_email", "count"),
    ).reset_index()
    summary_path = OUT_DIR / "_summary.csv"
    summary.to_csv(summary_path, index=False)

    print(f"\nSkipped (no website / no match in enriched file): {skipped}")
    print(f"Skipped (generic personal-email domain): {domain_blocked}")
    print(f"\nUnique people patterned: {out_long['linkedin_url'].nunique()}")
    print(f"Total candidate emails: {len(out_long)}")
    print(f"Unique candidate emails: {out_long['candidate_email'].nunique()}")
    print(f"\nPer ICP/persona:")
    print(summary.to_string(index=False))
    print(f"\nOutputs:")
    print(f"  {long_path}")
    print(f"  {flat_path}")
    print(f"  {summary_path}")


if __name__ == "__main__":
    main()
