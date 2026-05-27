# CLAUDE.md — uk_cro_shopify

Project-specific guidance for Claude Code when working in `projects/uk_cro_shopify/`.

## What this project is

Scrape all UK CRO agencies that list Shopify as a service on Clutch.co. Deliverable: a clean CSV of agency profiles. No contact enrichment in scope yet.

## Layout

```
inputs/     # drop any manually exported files here (e.g. Clutch HTML snapshots)
scripts/    # numbered pipeline scripts
  01_scrape_clutch.py   # Apify scrape → output/raw_clutch.json
  02_clean_export.py    # JSON → normalised CSV
output/
  raw_clutch.json               # raw Apify actor output (do not edit)
  uk_cro_shopify_agencies.csv   # canonical deliverable
```

## Scraping approach

1. **Preferred**: Apify actor. Search for a Clutch scraper on Apify store. Auth via `APIFY_API_TOKEN`.
   - Start actor run with target URL filtered to UK + Shopify service.
   - Poll until status `SUCCEEDED`, then download dataset as JSON.
   - Save to `output/raw_clutch.json`.

2. **Fallback**: Playwright headless scrape if no reliable Apify actor exists.
   - Use `playwright` (sync API, Chromium).
   - Paginate: increment `?page=N` until the result list is empty.
   - Rate-limit: 2 s delay between page requests, respect `robots.txt`.

## Script conventions

- Load API keys via `from lib.config import get_key` (repo root `lib/`).
- Read/write CSVs with `pandas.read_csv(..., dtype=str, keep_default_na=False)`.
- Scripts are resumable: check if `output/raw_clutch.json` already exists before re-running the actor.
- Print progress to stdout (page count, agency count so far).

## Env vars needed

| Var | Used for |
|---|---|
| `APIFY_API_TOKEN` | Already in `.env` (shared with uk_smb_4icp) |

No new env vars required unless a paid Clutch API is introduced.

## Out of scope (for now)

- Contact enrichment (Prospeo / Icypeas) — add as a new numbered script if client requests it.
- Adyntel / SimilarWeb signals — same.
- Lead scoring — same.
