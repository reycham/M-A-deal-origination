"""Personalised cold-email opener generator (Smartlead-ready) — deterministic templates.

Two phases:
  Phase 1 — for each unique company (de-duped by linkedin_norm), pick a branch
            from the available signals (Meta ads / Google ads / organic search /
            fallback) and emit one of six fixed sentence templates. No LLM. No
            cost. Output: output/openers/openers.csv.
  Phase 2 — left-join openers + company_short into the 8 final_v5 files →
            output/final_v6/<ICP> - <persona> with opener.csv, plus
            _all_deliverable_ranked.csv + _summary.csv.

The `company_short` column is preserved from the existing openers.csv (which
was cleaned by scripts 14 and 15). If openers.csv doesn't exist, falls back
to the deterministic suffix-stripper for company_short.

Flags:
  --limit N        process only first N companies
  --no-resume      ignore existing openers.csv (rebuild company_short from scratch)
  --confirm        actually write (otherwise dry-run preview)
  --skip-merge     phase 1 only
  --merge-only     phase 2 only
  --input PATH     alt input CSV (default: final_v5/_all_deliverable_ranked.csv)
  --output PATH    alt output CSV path (default: output/openers/openers.csv)
  --seed N         random seed for fallback-template choice (default: 42)
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

FINAL_V5 = PROJECT_ROOT / "output" / "final_v5"
FINAL_V6 = PROJECT_ROOT / "output" / "final_v6"
OPENERS_DIR = PROJECT_ROOT / "output" / "openers"
MASTER_PATH = FINAL_V5 / "_all_deliverable_ranked.csv"
OPENERS_PATH = OPENERS_DIR / "openers.csv"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]
TRAFFIC_THRESHOLD = 5000

OUT_COLS = [
    "linkedin_norm", "company_name", "company_short",
    "branch", "opener", "status", "error",
]

TEMPLATES = {
    "both_ads":       "Came across {{company_short}} today and noticed you're running ads on both Meta and Google.",
    "meta_only":      "Came across {{company_short}} today and noticed you're running ads on Meta.",
    "google_only":    "Came across {{company_short}} today and noticed you're running ads on Google.",
    "organic_traffic":"Came across {{company_short}} today and noticed you're pulling decent traffic through search.",
}

FALLBACK_TEMPLATES = [
    "Came across {{company_short}} today and thought it was worth dropping you a message directly.",
    "Spotted {{company_short}} today and thought it was worth getting in touch.",
    "Came across {{company_short}} this morning and thought it was worth a message.",
]

# Suffix stripper for first-time company_short fallback (only used if openers.csv absent).
_SUFFIX_PATTERNS = [
    r"\(uk\)", r"\buk\b",
    r"holdings", r"holding", r"group",
    r"incorporated", r"corporation", r"corp\.?",
    r"limited", r"ltd\.?", r"llp\.?", r"plc\.?",
    r"inc\.?",
]
_SUFFIX_RE = re.compile(
    r"[\s,.&]+(?:" + "|".join(_SUFFIX_PATTERNS) + r")[\s,.]*$",
    re.IGNORECASE,
)


def normalize_company_name(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if not name:
        return ""
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX_RE.sub("", name).strip().rstrip(",.").strip()
    return name or raw.strip()


def norm_li(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def to_int(val) -> int:
    try:
        s = str(val).strip()
        if not s:
            return 0
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def pick_branch(meta: int, google: int, traffic: int) -> str:
    if meta > 0 and google > 0:
        return "both_ads"
    if meta > 0:
        return "meta_only"
    if google > 0:
        return "google_only"
    if traffic > TRAFFIC_THRESHOLD:
        return "organic_traffic"
    return "fallback"


def make_opener(branch: str, rng: random.Random) -> str:
    if branch == "fallback":
        return rng.choice(FALLBACK_TEMPLATES)
    return TEMPLATES[branch]


def build_input_rows(input_path: Path) -> list[dict]:
    """De-dup the input by linkedin_norm; keep the highest-scored row per company."""
    df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    df["_li"] = df["LinkedIn"].map(norm_li)
    df = df[df["_li"] != ""].copy()
    if "lead_score" in df.columns:
        df["_score"] = pd.to_numeric(df["lead_score"], errors="coerce").fillna(0)
        df = df.sort_values("_score", ascending=False)
    df = df.drop_duplicates("_li", keep="first")
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "linkedin_norm": r["_li"],
            "company_name": r.get("Company Name", ""),
            "meta_ads_count": r.get("meta_ads_count", ""),
            "google_ads_count": r.get("google_ads_count", ""),
            "sw_total_visits": r.get("sw_total_visits", ""),
        })
    return rows


def load_existing_short() -> dict[str, str]:
    """Return {linkedin_norm: company_short} from existing openers.csv if present."""
    if not OPENERS_PATH.exists():
        return {}
    df = pd.read_csv(OPENERS_PATH, dtype=str, keep_default_na=False)
    out: dict[str, str] = {}
    for _, r in df.iterrows():
        li = r.get("linkedin_norm")
        short = r.get("company_short", "")
        if li and short:
            out[li] = short
    return out


def phase1_run(args) -> None:
    OPENERS_DIR.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input) if args.input else MASTER_PATH
    all_rows = build_input_rows(input_path)
    if args.limit:
        all_rows = all_rows[: args.limit]

    existing_short = {} if args.no_resume else load_existing_short()
    rng = random.Random(args.seed)

    branch_counts: dict[str, int] = {}
    out_rows: list[dict] = []
    for r in all_rows:
        meta = to_int(r["meta_ads_count"])
        google = to_int(r["google_ads_count"])
        visits = to_int(r["sw_total_visits"])
        branch = pick_branch(meta, google, visits)
        branch_counts[branch] = branch_counts.get(branch, 0) + 1

        company_raw = r.get("company_name", "") or ""
        company_short = (existing_short.get(r["linkedin_norm"])
                         or normalize_company_name(company_raw)
                         or company_raw)
        opener = make_opener(branch, rng)

        out_rows.append({
            "linkedin_norm": r["linkedin_norm"],
            "company_name": company_raw,
            "company_short": company_short,
            "branch": branch,
            "opener": opener,
            "status": "ok",
            "error": "",
        })

    print(f"Input: {len(all_rows)} unique companies.")
    print("Branch mix:", ", ".join(f"{k}={v}" for k, v in sorted(branch_counts.items())))
    print(f"Preserved company_short from existing openers.csv: "
          f"{sum(1 for r in out_rows if r['linkedin_norm'] in existing_short)} / {len(out_rows)}")

    if not args.confirm:
        print("\nDry run — pass --confirm to write.")
        print("Sample first 5:")
        for r in out_rows[:5]:
            print(f"  [{r['branch']}] {r['company_short']}: {r['opener']}")
        return

    out_path = Path(args.output) if args.output else OPENERS_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(out_rows)[OUT_COLS].to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")


def phase2_merge() -> None:
    if not OPENERS_PATH.exists():
        sys.exit("Missing openers.csv — run phase 1 first.")
    op = pd.read_csv(OPENERS_PATH, dtype=str, keep_default_na=False)
    op = op[["linkedin_norm", "company_short", "opener"]].rename(
        columns={"opener": "personalized_opener"}
    ).drop_duplicates("linkedin_norm", keep="first")

    FINAL_V6.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    master_chunks = []

    for icp in ICPS:
        for persona in PERSONAS:
            in_path = FINAL_V5 / f"{icp} - {persona} with summary.csv"
            if not in_path.exists():
                print(f"[skip] {in_path.name}")
                continue
            df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
            df["_li_key"] = df["LinkedIn"].map(norm_li)
            n_in = len(df)
            df = df.merge(op, left_on="_li_key", right_on="linkedin_norm", how="left")
            df = df.drop(columns=["_li_key", "linkedin_norm"], errors="ignore")
            assert len(df) == n_in, f"row drift on {in_path.name}: {n_in}->{len(df)}"
            df["personalized_opener"] = df["personalized_opener"].fillna("")
            df["company_short"] = df["company_short"].fillna("")

            out_path = FINAL_V6 / f"{icp} - {persona} with opener.csv"
            df.to_csv(out_path, index=False)
            tier_counts = df["tier"].value_counts().to_dict()
            with_opener = (df["personalized_opener"] != "").sum()
            summary_rows.append({
                "icp": icp, "persona": persona, "rows": len(df),
                "Hot": tier_counts.get("Hot", 0), "Warm": tier_counts.get("Warm", 0),
                "Cool": tier_counts.get("Cool", 0), "Cold": tier_counts.get("Cold", 0),
                "X": tier_counts.get("X", 0),
                "with_opener": int(with_opener),
            })
            master_chunks.append(df[df["tier"].isin(["Hot", "Warm", "Cool", "Cold"])])
            print(f"[{icp} / {persona}] rows={len(df)}  with_opener={with_opener}")

    if master_chunks:
        ranked = pd.concat(master_chunks, ignore_index=True)
        ranked["_score"] = pd.to_numeric(ranked["lead_score"], errors="coerce").fillna(0)
        ranked = ranked.sort_values("_score", ascending=False).drop(columns=["_score"])
        KEY_COLS = [
            "tier", "lead_score", "Company Name", "company_short", "icp", "persona",
            "final_email", "personalized_opener",
            "founder_title", "founder_first_name", "founder_last_name",
            "meta_ads_count", "google_ads_count",
            "sw_total_visits", "sw_top_country", "sw_category",
            "Headcount", "Annual Revenue", "LinkedIn", "Website",
            "website_summary", "website_status",
        ]
        present = [c for c in KEY_COLS if c in ranked.columns]
        other = [c for c in ranked.columns if c not in present]
        ranked[present + other].to_csv(FINAL_V6 / "_all_deliverable_ranked.csv", index=False)

    pd.DataFrame(summary_rows).to_csv(FINAL_V6 / "_summary.csv", index=False)
    print(f"\nPhase 2 done. Wrote {FINAL_V6}/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--skip-merge", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    ap.add_argument("--input", type=str, default="")
    ap.add_argument("--output", type=str, default="")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.merge_only:
        phase2_merge()
        return

    src = Path(args.input) if args.input else MASTER_PATH
    if not src.exists():
        sys.exit(f"Missing input file: {src}")

    phase1_run(args)

    if not args.skip_merge and args.confirm:
        print("\n--- Phase 2: merge into final_v6 ---")
        phase2_merge()


if __name__ == "__main__":
    main()
