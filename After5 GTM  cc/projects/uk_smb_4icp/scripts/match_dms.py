"""Match each ICP company to its highest-authority Founder and Sales DMs.

Outputs 8 CSVs (one per ICP per persona) into ./output/ plus a summary file.
See CLAUDE.md and matching instructions.md for the brief.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "output"

ICPS = [
    (
        "Real estate",
        "4 icps/Real estate 1700.csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Real estate  Founder(7659).csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Real State Sales Rafi(463).csv",
    ),
    (
        "Mortgage",
        "4 icps/Mortgage first 1000.csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Mortgage Founder (1772).csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Mortgage Sales (182).csv",
    ),
    (
        "Dealership",
        "4 icps/Dealership first 1000.csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Dealership Founder(2263 ).csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - dealership Sales(183 Data).csv",
    ),
    (
        "Recruitment",
        "4 icps/Recruitment first 1000.csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Recruit Founder (10081).csv",
        "8 scrapes for 4 icps/Louis-2026-05-01(Apollo Data) - Recruit Sales (1061).csv",
    ),
]

SENIORITY_RANK = {
    "founder": 0, "owner": 1,
    "c_suite": 2, "c suite": 2, "cxo": 2,
    "partner": 3, "vp": 4, "head": 5, "director": 6,
    "manager": 7, "senior": 8, "entry": 9, "intern": 10, "": 99,
}
TITLE_BUMP_RE = re.compile(
    r"\b(founder|owner|ceo|chief executive|president|managing director|"
    r"cmo|cro|cso|vp sales|head of sales|head of marketing|"
    r"sales director|marketing director|director of sales|director of marketing)\b",
    re.I,
)
NAME_STOPWORDS = {
    "ltd", "limited", "llc", "inc", "incorporated", "plc", "pty", "group",
    "the", "co", "company", "corp", "corporation", "gmbh", "sa", "sarl", "ag", "bv",
}
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "mail.com",
    "btinternet.com", "sky.com", "ntlworld.com", "virginmedia.com",
}

# Output column blocks (persona-prefixed names assigned at merge time)
DM_FIELDS = [
    "first_name", "last_name", "title", "email",
    "mobile_phone", "work_direct_phone", "corporate_phone",
    "linkedin", "match_method",
]
APOLLO_SOURCE_COLS = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "title": "Title",
    "email": "Email",
    "mobile_phone": "Mobile Phone",
    "work_direct_phone": "Work Direct Phone",
    "corporate_phone": "Corporate Phone",
    "linkedin": "Person Linkedin Url",
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


def norm_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        return ""
    s = name.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t and t not in NAME_STOPWORDS]
    while tokens and tokens[-1] in NAME_STOPWORDS:
        tokens.pop()
    return " ".join(tokens)


def score_row(seniority: str, title: str) -> int:
    s = (seniority or "").strip().lower()
    base = SENIORITY_RANK.get(s, 50)
    if title and TITLE_BUMP_RE.search(title):
        base = max(0, base - 1)
    return base


def load_csv(rel_path: str) -> pd.DataFrame:
    path = ROOT / rel_path
    if not path.exists():
        sys.exit(f"Missing input file: {path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="replace")


def prep_scrape(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Return best-by-{linkedin, domain, name} tables — one row per company key."""
    df = df.copy()
    df["_li_key"] = df.get("Company Linkedin Url", "").map(norm_li)
    df["_name_key"] = df.get("Company Name", "").map(norm_name)
    # Domain key: prefer Apollo's Website, fall back to non-generic Email domain
    web_dom = df.get("Website", pd.Series([""] * len(df))).map(domain_from_url)
    email_dom = df.get("Email", pd.Series([""] * len(df))).map(domain_from_email)
    df["_dom_key"] = web_dom.where(web_dom != "", email_dom)
    df["_rank"] = [
        score_row(s, t) for s, t in zip(df.get("Seniority", ""), df.get("Title", ""))
    ]
    df = df.sort_values(["_rank", "Last Name"], kind="stable")

    return {
        "linkedin": df[df["_li_key"] != ""].drop_duplicates("_li_key", keep="first"),
        "domain":   df[df["_dom_key"] != ""].drop_duplicates("_dom_key", keep="first"),
        "name":     df[df["_name_key"] != ""].drop_duplicates("_name_key", keep="first"),
    }


def attach_dm(companies: pd.DataFrame, scrapes: dict[str, pd.DataFrame], persona: str) -> pd.DataFrame:
    out = companies.copy()
    for f in DM_FIELDS:
        out[f"{persona}_{f}"] = ""

    def lookup(scrape: pd.DataFrame, key: str) -> pd.DataFrame:
        cols = {key: scrape[key]}
        for f, src in APOLLO_SOURCE_COLS.items():
            cols[f"{persona}_{f}"] = scrape[src] if src in scrape.columns else ""
        return pd.DataFrame(cols)

    # Apply passes in priority order: LinkedIn → Domain → Name
    PASSES = [
        ("linkedin", "_li_key", "linkedin"),
        ("domain",   "_dom_key", "domain"),
        ("name",     "_name_key", "name"),
    ]
    for scrape_key, join_key, label in PASSES:
        needs = out[f"{persona}_match_method"] == ""
        if not needs.any():
            break
        scrape = scrapes[scrape_key]
        sub = out.loc[needs, [join_key]].copy()
        sub_idx = sub.index
        merged = sub.merge(lookup(scrape, join_key), on=join_key, how="left")
        merged.index = sub_idx
        hit_mask = (merged[f"{persona}_first_name"].fillna("") != "") & (out.loc[needs, join_key] != "")
        idx = hit_mask[hit_mask].index
        for f in APOLLO_SOURCE_COLS:
            col = f"{persona}_{f}"
            out.loc[idx, col] = merged.loc[idx, col].values
        out.loc[idx, f"{persona}_match_method"] = label

    out.loc[out[f"{persona}_match_method"] == "", f"{persona}_match_method"] = "none"
    return out


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    summary_rows = []

    for icp, comp_path, founder_path, sales_path in ICPS:
        print(f"[{icp}] loading...")
        companies = load_csv(comp_path)
        companies["_li_key"] = companies.get("LinkedIn", "").map(norm_li)
        companies["_name_key"] = companies.get("Company Name", "").map(norm_name)
        companies["_dom_key"] = companies.get("Website", "").map(domain_from_url)

        f_scrapes = prep_scrape(load_csv(founder_path))
        s_scrapes = prep_scrape(load_csv(sales_path))

        founders_out = attach_dm(companies, f_scrapes, "founder")
        sales_out = attach_dm(companies, s_scrapes, "sales")

        # Drop join-key helper columns from the output
        for df in (founders_out, sales_out):
            df.drop(columns=["_li_key", "_name_key", "_dom_key"], inplace=True, errors="ignore")

        f_path = OUT_DIR / f"{icp} - founders.csv"
        s_path = OUT_DIR / f"{icp} - sales.csv"
        founders_out.to_csv(f_path, index=False)
        sales_out.to_csv(s_path, index=False)

        for persona, df in (("founder", founders_out), ("sales", sales_out)):
            counts = df[f"{persona}_match_method"].value_counts().to_dict()
            summary_rows.append({
                "icp": icp,
                "persona": persona,
                "companies": len(df),
                "linkedin": counts.get("linkedin", 0),
                "domain": counts.get("domain", 0),
                "name_fallback": counts.get("name", 0),
                "no_match": counts.get("none", 0),
            })
            print(f"  {persona}: linkedin={counts.get('linkedin', 0)}  "
                  f"domain={counts.get('domain', 0)}  "
                  f"name={counts.get('name', 0)}  none={counts.get('none', 0)}")

    pd.DataFrame(summary_rows).to_csv(OUT_DIR / "_match_summary.csv", index=False)
    print(f"\nDone. Outputs in {OUT_DIR}")


if __name__ == "__main__":
    main()
