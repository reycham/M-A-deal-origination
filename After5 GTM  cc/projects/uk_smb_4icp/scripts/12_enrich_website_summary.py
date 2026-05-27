"""Website-summary enrichment for AI personalisation.

Two phases:
  Phase 1 — for each company in companies_to_enrich.csv: scrape homepage +
            about-page via Firecrawl, summarise with Claude Haiku 4.5, write
            output/signals/website_summaries.csv (resumable, concurrent).
  Phase 2 — left-join the summaries into output/final_v4/<ICP> - <persona>
            scored.csv → output/final_v5/<ICP> - <persona> with summary.csv,
            and rebuild _all_deliverable_ranked.csv + _summary.csv.

Flags:
  --limit N          process only first N companies (smoke test)
  --no-resume        re-query everything
  --workers N        concurrent companies (default 8)
  --confirm          actually call vendors (otherwise dry-run cost preview)
  --skip-merge       phase 1 only (skip the final_v5 rebuild)
  --merge-only       phase 2 only (use existing website_summaries.csv)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lib.config import get_key  # noqa: E402

import anthropic  # noqa: E402

SIGNALS_DIR = PROJECT_ROOT / "output" / "signals"
IN_PATH = SIGNALS_DIR / "companies_to_enrich.csv"
SUMMARIES_PATH = SIGNALS_DIR / "website_summaries.csv"
LOG_PATH = SIGNALS_DIR / "website_scrape_log.csv"
FINAL_V4 = PROJECT_ROOT / "output" / "final_v4"
FINAL_V5 = PROJECT_ROOT / "output" / "final_v5"

ICPS = ["Real estate", "Mortgage", "Dealership", "Recruitment"]
PERSONAS = ["founder", "sales"]
DELIVERABLE = {"ultra_sure", "very_sure", "probable"}

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
ABOUT_PATHS = ["/about", "/about-us"]  # 2 most common; fall back to homepage-only if both 404
PER_PAGE_CHAR_CAP = 4000
SUMMARY_MIN_SOURCE_CHARS = 200

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT = (
    "You summarise B2B company websites into 2–3 plain factual sentences for a sales rep. "
    "Capture: (a) what the company does, (b) who they serve / their target customer, "
    "(c) any notable specialism, scale, location, accreditation, or proof point that's "
    "specific to this company (not a generic statement). Avoid marketing fluff, "
    "superlatives, and emojis. If the source text is too thin to support a confident "
    "summary, output exactly the string INSUFFICIENT_CONTENT."
)

OUT_COLS = [
    "linkedin_norm", "company_name", "domain", "icp",
    "scraped_pages", "char_count", "summary", "status", "error",
]

FIRECRAWL_COST_PER_PAGE = 0.0015
HAIKU_COST_PER_COMPANY = 0.005

# Firecrawl returns 408/429/500 for transient failures
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 5         # ~120s total backoff window (4+8+16+32+60) — survives brief outages
TIMEOUT_SEC = 60


def firecrawl_scrape(url: str, api_key: str) -> dict:
    """Return {ok: bool, markdown: str, status: int, error: str}."""
    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # Backoff schedule: 4, 8, 16, 32, 60 seconds (~120s total — survives brief outages)
    backoffs = [4, 8, 16, 32, 60]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(FIRECRAWL_URL, json=body, headers=headers, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                return {"ok": False, "markdown": "", "status": 0, "error": str(e)[:200]}
            time.sleep(backoffs[attempt - 1])
            continue

        if r.status_code in RETRYABLE_STATUS:
            if attempt == MAX_RETRIES:
                return {"ok": False, "markdown": "", "status": r.status_code, "error": "retry exhausted"}
            time.sleep(backoffs[attempt - 1])
            continue

        if r.status_code != 200:
            return {"ok": False, "markdown": "", "status": r.status_code, "error": r.text[:200]}

        try:
            payload = r.json()
        except ValueError:
            return {"ok": False, "markdown": "", "status": r.status_code, "error": "non-json"}

        data = payload.get("data") or {}
        md = data.get("markdown") or ""
        if not md:
            return {"ok": False, "markdown": "", "status": 200, "error": "empty markdown"}
        return {"ok": True, "markdown": md, "status": 200, "error": ""}

    return {"ok": False, "markdown": "", "status": 0, "error": "exhausted"}


def scrape_company(domain: str, api_key: str) -> dict:
    """Try homepage + about (with fallbacks). Return concatenated markdown + audit."""
    pages: list[dict] = []
    combined_parts: list[str] = []

    # Homepage
    home_url = f"https://{domain}"
    home = firecrawl_scrape(home_url, api_key)
    pages.append({"url": home_url, "ok": home["ok"], "status": home["status"], "error": home["error"]})
    if home["ok"]:
        combined_parts.append(home["markdown"][:PER_PAGE_CHAR_CAP])

    # About page (try alternates until one works)
    about_md = ""
    for path in ABOUT_PATHS:
        about_url = f"https://{domain}{path}"
        about = firecrawl_scrape(about_url, api_key)
        pages.append({"url": about_url, "ok": about["ok"], "status": about["status"], "error": about["error"]})
        if about["ok"]:
            about_md = about["markdown"][:PER_PAGE_CHAR_CAP]
            break

    if about_md:
        combined_parts.append("---\n\n" + about_md)

    combined = "\n\n".join(combined_parts)
    return {"pages": pages, "combined": combined, "homepage_ok": home["ok"], "about_ok": bool(about_md)}


def summarise(company_name: str, source_text: str, anth_client: anthropic.Anthropic) -> dict:
    """Single Haiku call with cached system prompt + 429/transient-error retries."""
    if len(source_text) < SUMMARY_MIN_SOURCE_CHARS:
        return {"summary": "", "error": "thin_content"}
    user_msg = f"Company name: {company_name}\n\nWebsite content:\n\n{source_text}"
    last_err = ""
    for attempt in range(1, 6):  # up to 5 attempts for rate limits
        try:
            resp = anth_client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=200,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
            return {"summary": text, "error": ""}
        except anthropic.RateLimitError as e:
            last_err = f"rate_limit (attempt {attempt}): {str(e)[:120]}"
            time.sleep(min(60, 8 * attempt))  # 8, 16, 24, 32, 40s backoff
            continue
        except anthropic.APIConnectionError as e:
            last_err = f"connection (attempt {attempt}): {str(e)[:120]}"
            time.sleep(2 * attempt)
            continue
        except anthropic.APIStatusError as e:
            # Other 5xx — retry; 4xx (besides rate limit) — fail fast
            if 500 <= getattr(e, "status_code", 0) < 600:
                last_err = f"server_{e.status_code} (attempt {attempt}): {str(e)[:120]}"
                time.sleep(2 * attempt)
                continue
            return {"summary": "", "error": f"llm_error: {str(e)[:200]}"}
        except Exception as e:
            return {"summary": "", "error": f"llm_error: {str(e)[:200]}"}
    return {"summary": "", "error": f"llm_error: {last_err}"}


def enrich_one(row: dict, fc_key: str, anth_client: anthropic.Anthropic) -> tuple[dict, list[dict]]:
    li = row["linkedin_norm"]
    domain = (row.get("domain") or "").strip()
    company = row.get("company_name", "")
    icp = row.get("icp", "")

    if not domain:
        return ({
            "linkedin_norm": li, "company_name": company, "domain": "", "icp": icp,
            "scraped_pages": "[]", "char_count": 0, "summary": "",
            "status": "skipped_no_domain", "error": "",
        }, [])

    scrape = scrape_company(domain, fc_key)
    page_logs = [
        {"linkedin_norm": li, "domain": domain, "url": p["url"],
         "ok": p["ok"], "status": p["status"], "error": p["error"]}
        for p in scrape["pages"]
    ]

    if not scrape["combined"]:
        return ({
            "linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
            "scraped_pages": json.dumps([p["url"] for p in scrape["pages"] if p["ok"]]),
            "char_count": 0, "summary": "",
            "status": "site_unreachable", "error": "no pages scraped",
        }, page_logs)

    char_count = len(scrape["combined"])
    if char_count < SUMMARY_MIN_SOURCE_CHARS:
        return ({
            "linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
            "scraped_pages": json.dumps([p["url"] for p in scrape["pages"] if p["ok"]]),
            "char_count": char_count, "summary": "",
            "status": "thin_content", "error": "",
        }, page_logs)

    summ = summarise(company, scrape["combined"], anth_client)
    if summ["error"]:
        return ({
            "linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
            "scraped_pages": json.dumps([p["url"] for p in scrape["pages"] if p["ok"]]),
            "char_count": char_count, "summary": "",
            "status": "llm_error", "error": summ["error"],
        }, page_logs)

    if summ["summary"] == "INSUFFICIENT_CONTENT":
        status = "thin_content"
        summary_text = ""
    elif scrape["homepage_ok"] and scrape["about_ok"]:
        status = "ok"
        summary_text = summ["summary"]
    else:
        status = "homepage_only" if scrape["homepage_ok"] else "about_only"
        summary_text = summ["summary"]

    return ({
        "linkedin_norm": li, "company_name": company, "domain": domain, "icp": icp,
        "scraped_pages": json.dumps([p["url"] for p in scrape["pages"] if p["ok"]]),
        "char_count": char_count, "summary": summary_text,
        "status": status, "error": "",
    }, page_logs)


RETRY_ON_RESUME = {"llm_error"}  # site_unreachable usually means genuinely dead — skip on resume


def load_existing() -> dict[str, dict]:
    """Return only rows we don't want to retry. Transient failures get re-attempted."""
    if not SUMMARIES_PATH.exists():
        return {}
    df = pd.read_csv(SUMMARIES_PATH, dtype=str, keep_default_na=False)
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        li = row.get("linkedin_norm")
        if not li:
            continue
        if row.get("status") in RETRY_ON_RESUME:
            continue
        out[li] = row.to_dict()
    return out


def norm_li(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def phase1_run(args, fc_key: str, anth_client: anthropic.Anthropic) -> None:
    df_in = pd.read_csv(IN_PATH, dtype=str, keep_default_na=False)
    if args.limit:
        df_in = df_in.head(args.limit)

    existing = {} if args.no_resume else load_existing()
    log_rows: list[dict] = []
    if LOG_PATH.exists():
        log_rows = pd.read_csv(LOG_PATH, dtype=str, keep_default_na=False).to_dict("records")

    # Reload all rows for the output (we want to keep `ok`/`thin_content` rows)
    if SUMMARIES_PATH.exists() and not args.no_resume:
        all_prev = pd.read_csv(SUMMARIES_PATH, dtype=str, keep_default_na=False)
        all_prev = all_prev[~all_prev["status"].isin(RETRY_ON_RESUME)]
        out_rows: list[dict] = all_prev.to_dict("records")
    else:
        out_rows = []
    out_keys = {r["linkedin_norm"] for r in out_rows}

    todo_rows = [r for _, r in df_in.iterrows() if r["linkedin_norm"] not in out_keys]
    print(f"Input: {len(df_in)} companies. Already done: {len(existing)}. To process: {len(todo_rows)} "
          f"(workers={args.workers})")
    est = len(todo_rows) * (FIRECRAWL_COST_PER_PAGE * 1.7 + HAIKU_COST_PER_COMPANY)
    print(f"Estimated cost: ~${est:.2f}")

    if not todo_rows:
        return
    if not args.confirm:
        print("Dry run — pass --confirm to actually launch.")
        return

    lock = threading.Lock()
    completed = 0
    started = time.time()

    def write_checkpoint() -> None:
        pd.DataFrame(out_rows)[OUT_COLS].to_csv(SUMMARIES_PATH, index=False)
        pd.DataFrame(log_rows).to_csv(LOG_PATH, index=False)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(enrich_one, dict(r), fc_key, anth_client): r["linkedin_norm"] for r in todo_rows}
        for fut in as_completed(futures):
            try:
                out, logs = fut.result()
            except Exception as e:
                li = futures[fut]
                print(f"  [error] {li}: {e}")
                continue
            with lock:
                out_rows.append(out)
                log_rows.extend(logs)
                completed += 1
                if completed % 25 == 0:
                    elapsed = time.time() - started
                    rate = completed / elapsed
                    eta = (len(todo_rows) - completed) / rate if rate > 0 else 0
                    summary_preview = (out.get("summary") or "")[:80].replace("\n", " ")
                    print(f"  ...{completed}/{len(todo_rows)}  ({rate:.1f}/s, eta {eta/60:.1f} min)  "
                          f"last: {out['company_name']} [{out['status']}] {summary_preview}")
                    write_checkpoint()

    write_checkpoint()
    print(f"\nPhase 1 done. {completed} new companies processed in {(time.time()-started)/60:.1f} min")
    print(f"Wrote {SUMMARIES_PATH}")
    sm = pd.DataFrame(out_rows)
    print("\nStatus mix:")
    print(sm["status"].value_counts().to_string())


def phase2_merge() -> None:
    if not SUMMARIES_PATH.exists():
        sys.exit("Missing website_summaries.csv — run phase 1 first.")
    summ = pd.read_csv(SUMMARIES_PATH, dtype=str, keep_default_na=False)
    summ = summ[["linkedin_norm", "summary", "status"]].rename(
        columns={"summary": "website_summary", "status": "website_status"}
    ).drop_duplicates("linkedin_norm", keep="first")

    FINAL_V5.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    master_chunks = []

    for icp in ICPS:
        for persona in PERSONAS:
            in_path = FINAL_V4 / f"{icp} - {persona} scored.csv"
            if not in_path.exists():
                print(f"[skip] {in_path.name}")
                continue
            df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
            df["_li_key"] = df["LinkedIn"].map(norm_li)
            n_in = len(df)
            df = df.merge(summ, left_on="_li_key", right_on="linkedin_norm", how="left")
            df = df.drop(columns=["_li_key", "linkedin_norm"], errors="ignore")
            assert len(df) == n_in, f"row drift on {in_path.name}: {n_in}->{len(df)}"
            df["website_summary"] = df["website_summary"].fillna("")
            df["website_status"] = df["website_status"].fillna("")

            out_path = FINAL_V5 / f"{icp} - {persona} with summary.csv"
            df.to_csv(out_path, index=False)
            tier_counts = df["tier"].value_counts().to_dict()
            with_summary = (df["website_summary"] != "").sum()
            summary_rows.append({
                "icp": icp, "persona": persona, "rows": len(df),
                "Hot": tier_counts.get("Hot", 0), "Warm": tier_counts.get("Warm", 0),
                "Cool": tier_counts.get("Cool", 0), "Cold": tier_counts.get("Cold", 0),
                "X": tier_counts.get("X", 0),
                "with_website_summary": int(with_summary),
            })
            master_chunks.append(df[df["tier"].isin(["Hot", "Warm", "Cool", "Cold"])])
            print(f"[{icp} / {persona}] rows={len(df)}  with_summary={with_summary}")

    if master_chunks:
        ranked = pd.concat(master_chunks, ignore_index=True).sort_values("lead_score", ascending=False)
        KEY_COLS = [
            "tier", "lead_score", "Company Name", "icp", "persona", "final_email",
            "founder_title", "founder_first_name", "founder_last_name",
            "meta_ads_count", "google_ads_count",
            "sw_total_visits", "sw_top_country", "sw_category",
            "Headcount", "Annual Revenue", "LinkedIn", "Website",
            "website_summary", "website_status",
        ]
        present = [c for c in KEY_COLS if c in ranked.columns]
        other = [c for c in ranked.columns if c not in present]
        ranked[present + other].to_csv(FINAL_V5 / "_all_deliverable_ranked.csv", index=False)

    pd.DataFrame(summary_rows).to_csv(FINAL_V5 / "_summary.csv", index=False)
    print(f"\nPhase 2 done. Wrote {FINAL_V5}/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--skip-merge", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args()

    if args.merge_only:
        phase2_merge()
        return

    if not IN_PATH.exists():
        sys.exit(f"Missing {IN_PATH}. Run 07 first.")

    fc_key = get_key("FIRECRAWL_API_KEY")
    anth_key = get_key("ANTHROPIC_API_KEY")
    anth_client = anthropic.Anthropic(api_key=anth_key)

    phase1_run(args, fc_key, anth_client)

    if not args.skip_merge and args.confirm:
        print("\n--- Phase 2: merge into final_v5 ---")
        phase2_merge()


if __name__ == "__main__":
    main()
