"""LLM-driven brand-name normaliser for company_short.

The deterministic regex in script 13 leaves descriptors (Estate Agents,
Properties, Group, etc.) and TLD fragments (.com, .co.uk) on many names.
This pass uses Claude Haiku 4.5 with the company's website domain as a
hint to extract the brand stem only.

Reads:  output/openers/openers.csv  +  output/final_v5/_all_deliverable_ranked.csv (for domains)
Writes: output/openers/openers.csv  (overwrites company_short column in place)

After running this, re-run `13_generate_opener.py --merge-only` to push the
new short names through into output/final_v6/.

Flags:
  --limit N        process only first N companies (smoke test)
  --no-resume      re-query everything (otherwise only re-normalise rows
                   whose company_short looks "dirty")
  --workers N      concurrent calls (default 8)
  --confirm        actually call Haiku (otherwise dry-run cost preview)
  --force-all      re-normalise every row, not just the dirty ones
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

FINAL_V5 = PROJECT_ROOT / "output" / "final_v5"
OPENERS_PATH = PROJECT_ROOT / "output" / "openers" / "openers.csv"
MASTER_PATH = FINAL_V5 / "_all_deliverable_ranked.csv"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_COST_PER_CALL = 0.0006  # tiny input — system cached, user msg ~40 tokens

SYSTEM_PROMPT = (
    "You normalise UK / international company names to the brand-only short form "
    "for use as a token in cold emails. Given a raw company name and (optionally) "
    "their website domain, output ONLY the brand stem.\n\n"
    "Rules:\n"
    "- Strip legal suffixes: Ltd, Limited, LLP, PLC, Inc, Corp, Group, Holdings, Co.\n"
    "- Strip generic descriptor tail words that name the industry, not the brand: "
    "Estate Agents, Estate Agent, Property, Properties, Recruitment, Solutions, "
    "Services, Mortgages, Mortgage, Financial, Consultants, Consulting, Surveyors, "
    "Group, Holdings, Motors, Cars, Auto, Automotive, Lettings, Sales.\n"
    "- Strip TLD fragments at the end: .com, .co.uk, .co, .io, .net, .org.\n"
    "- Strip parenthetical clauses, slashes/pipes with descriptors, B-Corp / "
    "certification badges (e.g. '| Certified B Corp'), franchise tags.\n"
    "- Use the domain as a tiebreak: if the domain root matches a shorter form of "
    "the name (e.g. domain `martyngerrard.co.uk` and name 'Martyn Gerrard Estate "
    "Agents'), prefer the domain-aligned brand ('Martyn Gerrard').\n"
    "- Preserve casing and punctuation of the brand itself (Rise & Fall, KGM, "
    "Selina Finance). Keep ampersands, apostrophes, accented characters.\n"
    "- Never invent words not present in the raw name.\n"
    "- If stripping would leave nothing identifiable, keep more of the name.\n"
    "- Output the short name only. No quotes, no period at the end, no explanation, "
    "no leading/trailing whitespace."
)

OUT_COLS = [
    "linkedin_norm", "company_name", "company_short",
    "branch", "opener", "status", "error",
]

# Heuristic to flag rows that probably need re-normalising.
DIRTY_PATTERNS = [
    re.compile(r"\.(com|co\.uk|co|io|net|org)\b", re.IGNORECASE),
    re.compile(r"\b(Estate Agents?|Properties|Property|Recruitment|Solutions|"
               r"Services|Mortgages?|Financial|Consultants?|Consulting|Surveyors|"
               r"Group|Holdings?|Motors|Lettings|Limited|Ltd\.?|LLP|PLC|Inc\.?|"
               r"Holding|Auctions|Tax)\b", re.IGNORECASE),
    re.compile(r"[\(\|/\\]"),  # parens, pipes, slashes — usually carry descriptors
]


def is_dirty(short: str) -> bool:
    if not isinstance(short, str) or not short.strip():
        return True
    return any(p.search(short) for p in DIRTY_PATTERNS)


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
    t = re.sub(r"\s+", " ", t).strip().rstrip(".").strip()
    # Ignore obvious refusals / prefacing
    if t.lower().startswith(("here is", "the brand", "short name:", "brand:")):
        # Take after the colon if any
        if ":" in t:
            t = t.split(":", 1)[1].strip()
    return t


def normalise_one(raw_name: str, domain: str, client: anthropic.Anthropic) -> tuple[str, str]:
    parts = [f"Raw name: {raw_name}"]
    if domain:
        parts.append(f"Website domain: {domain}")
    parts.append("\nShort brand name:")
    msg = "\n".join(parts)
    text, err = call_haiku(client, msg)
    if err:
        return "", err
    short = clean(text)
    # Sanity guard: result letters must be derivable from raw name OR domain stem
    raw_blob = re.sub(r"\W+", "", raw_name.lower())
    domain_stem = re.sub(r"\.(com|co\.uk|co|io|net|org|uk).*$", "", domain.lower())
    domain_stem = re.sub(r"\W+", "", domain_stem)
    short_blob = re.sub(r"\W+", "", short.lower())
    if short_blob and short_blob not in raw_blob and short_blob not in domain_stem:
        # Fallback: every word of result must appear in raw OR domain
        raw_words = set(re.findall(r"\w+", raw_name.lower()))
        short_words = set(re.findall(r"\w+", short.lower()))
        if not short_words or not (short_words & (raw_words | {domain_stem})):
            return "", f"hallucinated: {short[:80]}"
    return short or raw_name, ""


def load_domains() -> dict[str, str]:
    df = pd.read_csv(MASTER_PATH, dtype=str, keep_default_na=False)
    out: dict[str, str] = {}
    for _, r in df.iterrows():
        li = r.get("LinkedIn", "")
        if not isinstance(li, str) or not li.strip():
            continue
        u = li.strip().lower()
        u = re.sub(r"^https?://", "", u)
        u = re.sub(r"^www\.", "", u)
        u = u.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        domain = (r.get("Website") or "").strip()
        if u and u not in out and domain:
            out[u] = domain
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--force-all", action="store_true",
                    help="re-normalise every row, not just dirty ones")
    args = ap.parse_args()

    if not OPENERS_PATH.exists():
        sys.exit(f"Missing {OPENERS_PATH}. Run script 13 first.")

    df = pd.read_csv(OPENERS_PATH, dtype=str, keep_default_na=False)
    domains = load_domains()
    df["_domain"] = df["linkedin_norm"].map(lambda x: domains.get(x, ""))

    if args.force_all:
        todo_mask = pd.Series([True] * len(df))
    else:
        todo_mask = df["company_short"].map(is_dirty)
    todo_idx = df.index[todo_mask].tolist()

    if args.limit:
        todo_idx = todo_idx[: args.limit]

    print(f"Total rows: {len(df)}. Need re-normalising: {len(todo_idx)} "
          f"(workers={args.workers})")
    est = len(todo_idx) * HAIKU_COST_PER_CALL
    print(f"Estimated cost: ~${est:.2f}")

    if not todo_idx:
        return
    if not args.confirm:
        # Show first 10 dirty rows so user can sanity-check
        print("\nFirst 10 candidate rows (raw -> current short -> domain):")
        for i in todo_idx[:10]:
            r = df.loc[i]
            print(f"  {r['company_name'][:50]!s:<52}  ->  {r['company_short'][:35]!s:<37}  ({r['_domain']})")
        print("\nDry run — pass --confirm to actually launch.")
        return

    anth_key = get_key("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=anth_key)

    lock = threading.Lock()
    completed = 0
    started = time.time()
    errors: list[tuple[int, str]] = []

    def worker(i: int) -> tuple[int, str, str]:
        r = df.loc[i]
        new_short, err = normalise_one(r["company_name"], r["_domain"], client)
        return i, new_short, err

    def write_checkpoint() -> None:
        df.drop(columns=["_domain"], errors="ignore")[OUT_COLS].to_csv(OPENERS_PATH, index=False)

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
                    df.at[i, "company_short"] = new_short
                completed += 1
                if completed % 50 == 0:
                    elapsed = time.time() - started
                    rate = completed / elapsed
                    eta = (len(todo_idx) - completed) / rate if rate > 0 else 0
                    r = df.loc[i]
                    print(f"  ...{completed}/{len(todo_idx)}  ({rate:.1f}/s, eta {eta/60:.1f} min)  "
                          f"last: {r['company_name'][:40]!s} -> {r['company_short'][:35]!s}")
                    write_checkpoint()

    write_checkpoint()
    print(f"\nDone. {completed} normalised in {(time.time()-started)/60:.1f} min")
    print(f"Wrote {OPENERS_PATH}")
    if errors:
        print(f"\n{len(errors)} errors (first 10):")
        for i, e in errors[:10]:
            print(f"  row {i} ({df.at[i,'company_name'][:40]}): {e}")
    print("\nNext step: re-run `python projects/uk_smb_4icp/scripts/13_generate_opener.py --merge-only` "
          "to push the new short names into output/final_v6/.")


if __name__ == "__main__":
    main()
