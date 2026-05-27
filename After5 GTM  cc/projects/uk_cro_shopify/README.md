# UK CRO Agencies — Shopify Specialists

Status: **in progress**. Goal: scrape all UK-based CRO agencies from Clutch.co that list Shopify as a service.

## Brief

Client needs a clean list of UK CRO agencies that work with Shopify merchants. Source: Clutch.co filtered by location=UK, service=Conversion Optimisation, platform=Shopify.

Output: flat CSV of agencies — name, Clutch URL, website, description, rating, reviews count, min project size, hourly rate, location.

No contact enrichment in scope for now (just the agency list).

## Target URL

```
https://clutch.co/uk/agencies/conversion-optimization?filter[services][0]=shopify
```

Paginate through all result pages until no more agencies appear.

## Pipeline (run in order from `scripts/`)

| # | Script | Purpose |
|---|---|---|
| 1 | `01_scrape_clutch.py` | Scrape agency listings from Clutch via Apify actor. Output: `output/raw_clutch.json` |
| 2 | `02_clean_export.py` | Normalise JSON → flat CSV, deduplicate, filter to UK-only. Output: `output/uk_cro_shopify_agencies.csv` |

## Output schema (`output/uk_cro_shopify_agencies.csv`)

| Column | Notes |
|---|---|
| `name` | Agency name |
| `clutch_url` | Full Clutch profile URL |
| `website` | Agency website domain |
| `tagline` | Short Clutch tagline |
| `description` | Clutch profile description |
| `rating` | Clutch star rating (float) |
| `reviews_count` | Number of Clutch reviews |
| `min_project_size` | e.g. "$1,000+" |
| `hourly_rate` | e.g. "$50–$99 / hr" |
| `location` | City, UK |
| `employees` | Employee band e.g. "10–49" |
| `founded` | Year founded |
| `services` | Comma-separated service tags from Clutch |

## Vendor / tool

Apify actor: `apify/clutch-scraper` (or equivalent). Auth via `APIFY_API_TOKEN` in `.env`.
Fallback: Playwright headless browser if no suitable Apify actor exists.
