"""LLM brand-name normaliser v2 — fresh pass with the user's full ruleset.

Reads:  output/openers/openers.csv  +  final_v6/_all_deliverable_ranked.csv
        (latter for founder_first_name / founder_last_name lookup per company)
Writes: output/openers/openers.csv (overwrites company_short column in place)
        + output/openers/_company_short_v2_diff.csv  (before/after diff for review)

After running, re-run `13_generate_opener.py --merge-only` to push the new
short names through into output/final_v6/.

Flags:
  --limit N      process only first N rows (smoke test)
  --workers N    concurrent calls (default 8)
  --confirm      actually write (otherwise dry run with cost estimate)
  --dry-print N  in dry run, print the first N candidate inputs (default 10)
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lib.config import get_key  # noqa: E402

import anthropic  # noqa: E402

OPENERS_PATH = PROJECT_ROOT / "output" / "openers" / "openers.csv"
MASTER_PATH = PROJECT_ROOT / "output" / "final_v6" / "_all_deliverable_ranked.csv"
DIFF_PATH = PROJECT_ROOT / "output" / "openers" / "_company_short_v2_diff.csv"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
# Anthropic Haiku 4.5 pricing (2026): $1/MTok input, $5/MTok output, $0.10/MTok cache read,
# $1.25/MTok cache write. System prompt ~1.4K tokens cached after 1st call.
HAIKU_COST_PER_CALL = 0.0002

SYSTEM_PROMPT = (
    "You normalise UK company names to the best short form for use in a cold "
    "email opening line.\n\n"
    "You are given:\n"
    "- Raw company name: {{company_name}}\n"
    "- Contact first name: {{founder_first_name}}\n"
    "- Contact last name: {{founder_last_name}}\n\n"
    "Output ONLY the short name. Plain text. No symbols. No emojis. No quotes. "
    "No explanation. No punctuation at the end. Natural capitalisation.\n\n"
    "RULES — apply in this order:\n\n"
    "RULE 1 — CONTACT NAME CHECK.\n"
    "If the contact's first name OR last name appears anywhere in the raw "
    "company name, output exactly:\n"
    "your company\n"
    "Examples:\n"
    "First: Peter / Last: Clark / Company: Peter Clark Estate Agents → your company\n"
    "First: Jonathan / Last: Lee / Company: Jonathan Lee Recruitment → your company\n"
    "First: Michael / Last: Poole / Company: Michael Poole Lettings → your company\n"
    "Only apply this if first_name or last_name is not empty.\n\n"
    "RULE 2 — STRIP ALL SYMBOLS AND EMOJIS.\n"
    "Remove: ™ ® © ≈ † ★ ⭐ 🏆 and any non-standard characters, emoji sequences, "
    "star ratings.\n"
    "If stripping reveals a recognisable brand reconstruct it.\n"
    "\"Caffyns ≈†KODA\" → \"Caffyns Skoda\"\n\n"
    "RULE 3 — STRIP PIPE OR DASH SEPARATED DESCRIPTORS.\n"
    "Remove everything after | or — when followed by a descriptor, certification, "
    "or network tag.\n"
    "But if what comes after the | or — is a more specific local brand name, use "
    "that instead of the parent brand.\n"
    "\"Selina Finance | Certified B Corp\" → \"Selina Finance\"\n"
    "\"Mortgage Advice Bureau - Brook Financial Services\" → \"Brook Financial\"\n"
    "\"Mortgage Advice Bureau - Network Partner\" → \"Mortgage Advice Bureau\"\n\n"
    "RULE 4 — STRIP LEGAL SUFFIXES.\n"
    "Remove: Ltd, Limited, LLP, PLC, Inc, Corp, Co.\n\n"
    "RULE 5 — STRIP GENERIC INDUSTRY DESCRIPTOR TAIL WORDS.\n"
    "Only strip if something meaningful remains after stripping.\n"
    "Strip: Estate Agents, Estate Agent, Letting Agents, Lettings, Property, "
    "Properties, Recruitment, Solutions, Services, Mortgages, Mortgage, "
    "Financial, Consultants, Consulting, Surveyors, Holdings, Homes, Realty, "
    "Motors, Cars, Automotive, Auto, Management, Advisors, Advisers, Associates, "
    "Partners, Group, Brokers, Insurance Brokers, Wealth Management, Asset "
    "Management, Specialist Finance, Finance, Developments, Network Partner.\n\n"
    "RULE 6 — ABBREVIATE LONG NAMES.\n"
    "If after stripping the name is still 4 or more words, abbreviate to initials "
    "in capitals.\n"
    "\"The London Management Company\" → \"TLMC\"\n"
    "\"Buckinghamshire Building Society\" → \"BBS\"\n"
    "\"Independent Mortgage Advice Bureau\" → \"IMAB\"\n"
    "\"HQ Mortgage and Finance\" → \"HQMF\"\n"
    "\"Jonathan Lee Recruitment\" → \"JLR\"\n"
    "\"United Business Finance\" → \"UBF\"\n"
    "\"BDWM Wealth Management\" → \"BDWM\"\n"
    "\"TAT Asset Management\" → \"TAT\"\n"
    "If an abbreviation already appears in the raw name, use it directly.\n\n"
    "RULE 7 — SINGLE STRONG BRAND WORDS.\n"
    "If after stripping only one strong word remains, use it.\n"
    "\"Truffle Specialist Finance\" → \"Truffle\"\n"
    "\"Acorn Wealth Management\" → \"Acorn\"\n"
    "\"Habito\" → \"Habito\"\n"
    "\"Houst\" → \"Houst\"\n\n"
    "RULE 8 — DOMAIN STYLE NAMES.\n"
    "Strip TLD and make readable.\n"
    "\"lettingaproperty.com\" → \"Letting A Property\"\n"
    "\"Loan.co.uk\" → \"Loan\"\n"
    "\"driveJohnson's\" → \"Drive Johnson's\"\n\n"
    "RULE 9 — BRAND PLUS MANUFACTURER OR LOCATION.\n"
    "For dealerships or franchise locations keep both.\n"
    "Strip \"Motor Cars\", \"Centre\", \"Select\", \"Cars\".\n"
    "\"Rolls-Royce Motor Cars Sunningdale\" → \"Rolls-Royce Sunningdale\"\n"
    "\"Aston Martin Nottingham\" → \"Aston Martin Nottingham\"\n"
    "\"Porsche Centre Solihull\" → \"Porsche Solihull\"\n"
    "\"Sytner Select Warrington\" → \"Sytner Warrington\"\n"
    "\"Caffyns Volvo\" → \"Caffyns Volvo\"\n"
    "\"Caffyns Volkswagen\" → \"Caffyns VW\"\n"
    "\"Spirit Hyundai & MG\" → \"Spirit Hyundai\"\n\n"
    "RULE 10 — MATCH ME STYLE NAMES.\n"
    "If the name is 3 words and forms a clear phrase, keep it.\n"
    "\"Match Me Car Finance\" → \"Match Me\"\n\n"
    "RULE 11 — IF NOTHING MEANINGFUL REMAINS.\n"
    "Keep more of the original name rather than outputting something confusing.\n\n"
    "RULE 12 — IF COMPLETELY UNCLEAR.\n"
    "Output: your company\n\n"
    "OUTPUT RULES:\n"
    "- Plain text only\n"
    "- No symbols, emojis, quotes, or punctuation at the end\n"
    "- Natural capitalisation — first letter capitalised, rest lowercase unless "
    "it is an abbreviation\n"
    "- Output the short name only and nothing else"
)

OUT_COLS = [
    "linkedin_norm", "company_name", "company_short",
    "branch", "opener", "status", "error",
]


def norm_li(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def load_founder_names() -> dict[str, tuple[str, str]]:
    """Map linkedin_norm -> (first, last) of the founder for that company."""
    df = pd.read_csv(MASTER_PATH, dtype=str, keep_default_na=False)
    df["_li"] = df["LinkedIn"].map(norm_li)
    out: dict[str, tuple[str, str]] = {}
    # Prefer founder rows; fall back to sales if no founder
    for _, r in df.iterrows():
        li = r["_li"]
        if not li:
            continue
        first = (r.get("founder_first_name") or "").strip()
        last = (r.get("founder_last_name") or "").strip()
        if li in out and out[li][0]:  # already have a founder name
            continue
        if first or last:
            out[li] = (first, last)
        elif li not in out:
            sf = (r.get("sales_first_name") or "").strip()
            sl = (r.get("sales_last_name") or "").strip()
            out[li] = (sf, sl)
    return out


def call_haiku(client: anthropic.Anthropic, user_msg: str) -> tuple[str, str]:
    last_err = ""
    for attempt in range(1, 6):
        try:
            resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=40,
                temperature=0,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
            return text, ""
        except anthropic.RateLimitError as e:
            last_err = f"rate_limit (attempt {attempt}): {str(e)[:120]}"
            time.sleep(min(60, 8 * attempt))
        except anthropic.APIConnectionError as e:
            last_err = f"connection (attempt {attempt}): {str(e)[:120]}"
            time.sleep(2 * attempt)
        except anthropic.APIStatusError as e:
            if 500 <= getattr(e, "status_code", 0) < 600:
                last_err = f"server_{e.status_code} (attempt {attempt}): {str(e)[:120]}"
                time.sleep(2 * attempt)
                continue
            return "", f"llm_error: {str(e)[:200]}"
        except Exception as e:  # noqa: BLE001
            return "", f"llm_error: {str(e)[:200]}"
    return "", f"llm_error: {last_err}"


def clean(text: str) -> str:
    t = text.strip()
    for q in ('"', "'", "“", "”", "‘", "’", "`"):
        if t.startswith(q):
            t = t[1:]
        if t.endswith(q):
            t = t[:-1]
    t = re.sub(r"\s+", " ", t).strip().rstrip(".,;:").strip()
    if t.lower().startswith(("here is", "the brand", "short name:", "brand:")):
        if ":" in t:
            t = t.split(":", 1)[1].strip()
    return t


def normalise_one(raw: str, first: str, last: str,
                  client: anthropic.Anthropic) -> tuple[str, str]:
    user_msg = (
        f"Raw company name: {raw}\n"
        f"Contact first name: {first}\n"
        f"Contact last name: {last}\n\n"
        f"Short name:"
    )
    text, err = call_haiku(client, user_msg)
    if err:
        return "", err
    short = clean(text)
    return short or raw, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--dry-print", type=int, default=10)
    args = ap.parse_args()

    if not OPENERS_PATH.exists():
        sys.exit(f"Missing {OPENERS_PATH}.")
    if not MASTER_PATH.exists():
        sys.exit(f"Missing {MASTER_PATH}. Re-run script 13 phase 2 first.")

    df = pd.read_csv(OPENERS_PATH, dtype=str, keep_default_na=False)
    names = load_founder_names()

    df["_first"] = df["linkedin_norm"].map(lambda x: names.get(x, ("", ""))[0])
    df["_last"] = df["linkedin_norm"].map(lambda x: names.get(x, ("", ""))[1])

    todo_idx = list(df.index)
    if args.limit:
        todo_idx = todo_idx[: args.limit]

    print(f"Total rows: {len(df)}. Will normalise: {len(todo_idx)} (workers={args.workers})")
    est = len(todo_idx) * HAIKU_COST_PER_CALL
    print(f"Estimated cost: ~${est:.3f} (Haiku 4.5 with cached system prompt)")

    if not args.confirm:
        print(f"\nFirst {args.dry_print} candidate inputs:")
        for i in todo_idx[: args.dry_print]:
            r = df.loc[i]
            print(f"  raw={r['company_name'][:50]!r:<52}  first={r['_first']!r:<20}  last={r['_last']!r}")
        print("\nDry run — pass --confirm to launch.")
        return

    anth_key = get_key("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=anth_key)

    lock = threading.Lock()
    completed = 0
    started = time.time()
    errors: list[tuple[int, str]] = []
    new_shorts: dict[int, str] = {}

    def worker(i: int) -> tuple[int, str, str]:
        r = df.loc[i]
        new_short, err = normalise_one(r["company_name"], r["_first"], r["_last"], client)
        return i, new_short, err

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(worker, i): i for i in todo_idx}
        for fut in as_completed(futures):
            try:
                i, new_short, err = fut.result()
            except Exception as e:  # noqa: BLE001
                errors.append((futures[fut], str(e)[:120]))
                continue
            with lock:
                if err:
                    errors.append((i, err))
                elif new_short:
                    new_shorts[i] = new_short
                completed += 1
                if completed % 25 == 0:
                    elapsed = time.time() - started
                    rate = completed / elapsed
                    eta = (len(todo_idx) - completed) / rate if rate > 0 else 0
                    print(f"  ...{completed}/{len(todo_idx)}  ({rate:.1f}/s, eta {eta/60:.1f} min)", flush=True)

    # Build diff and apply
    diff_rows = []
    for i, new in new_shorts.items():
        old = df.at[i, "company_short"]
        diff_rows.append({
            "linkedin_norm": df.at[i, "linkedin_norm"],
            "company_name": df.at[i, "company_name"],
            "first_name": df.at[i, "_first"],
            "last_name": df.at[i, "_last"],
            "old_short": old,
            "new_short": new,
            "changed": old != new,
        })
        df.at[i, "company_short"] = new

    df.drop(columns=["_first", "_last"], errors="ignore")[OUT_COLS].to_csv(OPENERS_PATH, index=False)
    pd.DataFrame(diff_rows).to_csv(DIFF_PATH, index=False)

    n_changed = sum(1 for r in diff_rows if r["changed"])
    n_your = sum(1 for r in diff_rows if r["new_short"].lower() == "your company")
    print(f"\nDone. {completed} normalised in {(time.time()-started)/60:.1f} min")
    print(f"Changed company_short on {n_changed} rows")
    print(f"'your company' results: {n_your}")
    print(f"Wrote {OPENERS_PATH}")
    print(f"Wrote diff: {DIFF_PATH}")
    if errors:
        print(f"\n{len(errors)} errors (first 5):")
        for i, e in errors[:5]:
            print(f"  row {i} ({df.at[i,'company_name'][:40]}): {e}")
    print("\nNext: python projects/uk_smb_4icp/scripts/13_generate_opener.py --merge-only")


if __name__ == "__main__":
    main()
