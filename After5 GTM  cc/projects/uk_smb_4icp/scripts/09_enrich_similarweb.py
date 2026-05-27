"""SimilarWeb traffic enrichment via Apify pro100chok/similarweb-scraper.

Batched bulk enrichment: actor accepts max 50 domains per run (min 10).
We chunk all unique domains into batches and run several concurrently.
Per-batch state cached to similarweb_batches.json so reruns skip completed batches.

Pricing: $2 per 1,000 results (~$3.34 for 1,670 domains).

Output mapping (actor field → our column):
  EstimatedMonthlyVisits[<latest>] -> sw_total_visits
  Engagments.BounceRate            -> sw_bounce_rate    (note: 0-100 in this actor)
  Engagments.PagePerVisit          -> sw_pages_per_visit
  Engagments.TimeOnSite            -> sw_time_on_site
  GlobalRank.Rank                  -> sw_global_rank
  Category                         -> sw_category
  TopCountryShares[0].CountryCode  -> sw_top_country

Flags:
  --limit N       only enrich the first N unique domains (smoke test)
  --batch-size N  domains per actor run (default 50, max 50, min 10)
  --workers N     concurrent batches in flight (default 5)
  --confirm       actually launch (otherwise dry-run cost preview)
  --reset         clear cached batch state and start over
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lib.config import get_key  # noqa: E402

SIGNALS_DIR = PROJECT_ROOT / "output" / "signals"
IN_PATH = SIGNALS_DIR / "companies_to_enrich.csv"
STATE_PATH = SIGNALS_DIR / "similarweb_batches.json"
OUT_PATH = SIGNALS_DIR / "similarweb_results.csv"

APIFY_TOKEN = get_key("APIFY_API_TOKEN")
ACTOR_ID = get_key("APIFY_SIMILARWEB_ACTOR_ID")

BATCH_MIN = 10
BATCH_MAX = 50
DEFAULT_WORKERS = 5
POLL_INTERVAL_SEC = 5
RUN_TIMEOUT_SEC = 600  # 10 min cap per batch
COST_PER_RESULT = 0.002


def launch_run(domains: list[str]) -> dict:
    r = requests.post(
        f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={APIFY_TOKEN}",
        json={"searchType": "similarweb", "domains": domains},
        timeout=60,
    )
    r.raise_for_status()
    d = r.json()["data"]
    return {"run_id": d["id"], "dataset_id": d["defaultDatasetId"]}


def poll_run(run_id: str) -> str:
    deadline = time.time() + RUN_TIMEOUT_SEC
    while time.time() < deadline:
        r = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}", timeout=30)
        r.raise_for_status()
        status = r.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return status
        time.sleep(POLL_INTERVAL_SEC)
    return "TIMEOUT"


def fetch_dataset(dataset_id: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    page = 1000
    while True:
        url = (f"https://api.apify.com/v2/datasets/{dataset_id}/items"
               f"?clean=1&format=json&limit={page}&offset={offset}&token={APIFY_TOKEN}")
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return items


def latest_visit(emv) -> int | str:
    """EstimatedMonthlyVisits is {date_str: visits}. Return most-recent value."""
    if not isinstance(emv, dict) or not emv:
        return ""
    try:
        latest_key = max(emv.keys())
        return int(emv[latest_key])
    except (ValueError, TypeError):
        return ""


def parse_top_country(arr) -> str:
    if isinstance(arr, list) and arr:
        return arr[0].get("CountryCode", "") or ""
    return ""


def flatten(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        eng = it.get("Engagments") or {}
        gr = it.get("GlobalRank") or {}
        visits = latest_visit(it.get("EstimatedMonthlyVisits"))
        out.append({
            "domain": (it.get("SiteName") or "").lower(),
            "sw_total_visits": visits,
            "sw_bounce_rate": eng.get("BounceRate", ""),
            "sw_pages_per_visit": eng.get("PagePerVisit", ""),
            "sw_time_on_site": eng.get("TimeOnSite", ""),
            "sw_global_rank": gr.get("Rank", "") if isinstance(gr, dict) else "",
            "sw_category": it.get("Category", "") or "",
            "sw_top_country": parse_top_country(it.get("TopCountryShares")),
            "sw_status": "ok" if (isinstance(visits, int) and visits > 0) else "no_data",
        })
    return out


def chunk_domains(domains: list[str], size: int) -> list[list[str]]:
    chunks = []
    for i in range(0, len(domains), size):
        chunk = domains[i:i + size]
        chunks.append(chunk)
    # If the trailing chunk is below the actor's minimum, merge it into the previous chunk
    if len(chunks) >= 2 and len(chunks[-1]) < BATCH_MIN:
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + tail
    return chunks


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"batches": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def process_batch(batch_idx: int, domains: list[str]) -> dict:
    """Run one actor invocation for `domains`. Returns batch result dict."""
    started = time.time()
    try:
        run = launch_run(domains)
    except Exception as e:
        return {"batch_idx": batch_idx, "domains": domains, "status": "launch_error",
                "error": str(e)[:300], "items": [], "elapsed_sec": time.time() - started}

    status = poll_run(run["run_id"])
    if status != "SUCCEEDED":
        return {"batch_idx": batch_idx, "domains": domains, "status": status,
                "run_id": run["run_id"], "dataset_id": run["dataset_id"],
                "error": f"run ended {status}", "items": [],
                "elapsed_sec": time.time() - started}

    try:
        items = fetch_dataset(run["dataset_id"])
    except Exception as e:
        return {"batch_idx": batch_idx, "domains": domains, "status": "fetch_error",
                "run_id": run["run_id"], "dataset_id": run["dataset_id"],
                "error": str(e)[:300], "items": [],
                "elapsed_sec": time.time() - started}

    return {"batch_idx": batch_idx, "domains": domains, "status": "ok",
            "run_id": run["run_id"], "dataset_id": run["dataset_id"],
            "items": items, "elapsed_sec": time.time() - started}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=BATCH_MAX)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if not IN_PATH.exists():
        sys.exit(f"Missing {IN_PATH}. Run 07 first.")

    df_in = pd.read_csv(IN_PATH, dtype=str, keep_default_na=False)
    domains = sorted({d.strip().lower() for d in df_in["domain"] if d.strip()})
    if args.limit:
        domains = domains[: args.limit]

    bs = max(BATCH_MIN, min(BATCH_MAX, args.batch_size))
    chunks = chunk_domains(domains, bs)
    est_cost = len(domains) * COST_PER_RESULT
    print(f"Unique domains: {len(domains)}")
    print(f"Batches: {len(chunks)} (size {bs})")
    print(f"Estimated cost: ${est_cost:.2f}  ({len(domains)} × ${COST_PER_RESULT})")

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("Reset state.")

    state = load_state()
    done_idxs = {b["batch_idx"] for b in state["batches"] if b.get("status") == "ok"}
    todo_chunks = [(i, c) for i, c in enumerate(chunks) if i not in done_idxs]
    print(f"Already done: {len(done_idxs)} batches | To run: {len(todo_chunks)}")

    if not todo_chunks:
        print("All batches done. Compiling results CSV.")
    elif not args.confirm:
        print("\nDry run — pass --confirm to actually launch.")
        return

    started_at = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_batch, i, c): i for i, c in todo_chunks}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                idx = futures[fut]
                print(f"  [batch {idx}] crashed: {e}")
                continue
            state["batches"].append(res)
            save_state(state)
            elapsed = res.get("elapsed_sec", 0)
            n_items = len(res.get("items", []))
            n_doms = len(res.get("domains", []))
            done_so_far = sum(1 for b in state["batches"] if b.get("status") == "ok")
            total_elapsed = time.time() - started_at
            rate = done_so_far / max(total_elapsed, 1) * 60  # batches/min
            print(f"  [batch {res['batch_idx']:>3}/{len(chunks)}] "
                  f"status={res['status']}  items={n_items}/{n_doms}  "
                  f"({elapsed:.0f}s)   total: {done_so_far}/{len(chunks)} batches "
                  f"({rate:.1f}/min)")

    # Compile final results CSV from all ok batches
    all_items = []
    for b in state["batches"]:
        if b.get("status") == "ok":
            all_items.extend(b.get("items", []))
    rows = flatten(all_items)
    df = pd.DataFrame(rows).drop_duplicates("domain", keep="first")
    df.to_csv(OUT_PATH, index=False)

    failed = [b for b in state["batches"] if b.get("status") != "ok"]
    print(f"\nWrote {OUT_PATH} ({len(df)} unique domain rows)")
    if failed:
        print(f"Failed batches: {len(failed)} — re-run script to retry them")
        for b in failed[:5]:
            print(f"  batch {b['batch_idx']}: {b.get('status')} {b.get('error','')[:120]}")


if __name__ == "__main__":
    main()
