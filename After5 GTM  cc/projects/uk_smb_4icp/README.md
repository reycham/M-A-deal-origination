# UK SMB 4-ICP Enrichment

Status: **complete with signals + scoring + website summaries (2026-05-04)**. Canonical deliverables in **`output/final_v5/`** (final_v4 + Haiku-generated `website_summary`). 1,862 deliverable contacts across 4 ICPs; **198 Hot, 879 Warm, 736 Cool, 49 Cold**. ~76% of companies have a usable website summary for AI personalisation.

## Brief

4 ICP company lists; 2 decision-makers per company (Founder/Owner + Sales/Marketing head). Apollo gave multiple people per company on the founder side and sparse sales coverage, so we pulled all contacts and deduped to highest-authority match here.

ICPs: Real Estate (1,662), Mortgage (1,000), Dealership (1,000), Recruitment (1,000).

## Inputs (`inputs/`)

- `4_icps/` — 4 ICP company CSVs (LinkedIn URL is the primary join key, 100% populated, unique).
- `8_scrapes/` — 8 Apollo contact exports (Founder + Sales × 4 ICPs). Filename casing inconsistent; one has a trailing space (`Founder(2263 ).csv`). Paths hardcoded in `scripts/match_dms.py`.
- `candidates_emails_only_FULL_REPORT_MILLIONVERIFIER.COM.csv` — MV bulk verification export.

## Pipeline (run in order from `scripts/`)

| # | Script | Purpose |
|---|---|---|
| 1 | `match_dms.py` | 3-pass join (LinkedIn → domain → name) + highest-authority pick |
| 2 | `enrich_emails.py` | Prospeo `enrich-person` for missing emails (LinkedIn-keyed, verified-only). Resumable. |
| 3 | `generate_email_patterns.py` | Build 6 candidate patterns from (first, last, domain) for the gap |
| 4 | `merge_verified.py` | Merge MV bulk results (`ok` / `catch_all`) |
| 5 | `finalize.py` | Consolidate Apollo + Prospeo + MV → `output/final/` + still-no-email gap files |
| 6 | `icypeas_pipeline.py` | Phase 1: search gap (recovered 7/576 — Apollo's UK SMB ceiling). Phase 2: verify all emails → `output/final_v2/` |
| 7 | `07_build_signals_input.py` | Filter to deliverable, dedup by LinkedIn URL → 1,691 unique companies in `output/signals/companies_to_enrich.csv` |
| 8 | `08_enrich_adyntel.py` | Per-company Meta + Google ad counts via Adyntel. 8–20-worker concurrent, resumable. ~$33 / 3,382 calls. |
| 9 | `09_enrich_similarweb.py` | Bulk SimilarWeb traffic via Apify `pro100chok/similarweb-scraper`. Batches of 50 (max), 5-worker concurrent runs. ~$3.34 / 1,670 domains in ~5 min. |
| 10 | `10_merge_signals.py` | Left-join Adyntel (on LinkedIn) + SimilarWeb (on domain) into final_v2 → `output/final_v3/` |
| 11 | `11_score_leads.py` | Weighted-sum 0–100 lead scoring + Hot/Warm/Cool/Cold tier (X = non-deliverable). Tunable `WEIGHTS` dict at top of script. → `output/final_v4/` |
| 12 | `12_enrich_website_summary.py` | Firecrawl scrapes homepage + /about → Haiku 4.5 produces 2-3 sentence factual summary. Resumable, concurrent. → **`output/final_v5/`** (canonical, adds `website_summary` column) |

Also: `pilot_name_domain.py` (one-off Prospeo experiment) and `verify_emails.py` (MV upload-poll-download; superseded by manual UI export).

## Final output

**`output/final_v4/<ICP> - <persona> scored.csv`** — every company row preserved; final_v3 columns + 2 new columns:

```
lead_score   int 0–100 (additive across 7 signal components)
tier         Hot / Warm / Cool / Cold  (deliverable rows)
             X                          (non-deliverable — scored for triage but not for email)
```

Plus aggregates:
- `_all_deliverable_ranked.csv` — 1,862 rows, master ranked list (deliverable only, sorted by score desc, key columns up front).
- `_summary.csv` — per-ICP × persona tier counts.
- `_score_distribution.csv` — score histogram for re-tuning thresholds.

Tier counts (deliverable only):

| Tier | Score band | Count | % of deliverable |
|---|---|---:|---:|
| Hot | ≥75 | 198 | 10.6% |
| Warm | 50–74 | 879 | 47.2% |
| Cool | 25–49 | 736 | 39.5% |
| Cold | <25 | 49 | 2.6% |

Scoring weights (all in `11_score_leads.py` `WEIGHTS` block — easy to retune):

| Component | Max | Notes |
|---|---:|---|
| Email certainty | 10 | ultra_sure 10 / very_sure 7 / probable 4 |
| Paid-ad activity | 25 | meta>0 +10, google>0 +10, +5 if heavy spender (meta>50 or google>100) |
| Traffic volume | 30 | bucketed: <500 → 0, 500–2k → 8, 2k–10k → 18, 10k–50k → 25, 50k+ → 30 |
| Engagement | 10 | +5 if bounce<50, +5 if pages>2 |
| Geo (UK) | 5 | sw_top_country == GB |
| DM seniority | 10 | +10 if founder/CEO/MD title; +5 if any founder match |
| Revenue band | 10 | UK SMB sweet spot ($500K–5M) +10 |

Known data quirk: 3 contacts have a platform domain (linktr.ee, google.com) as their `Website` because Apollo had it that way — their signals reflect the platform, not the company. Filter out manually if needed.

## Conventions

- `pandas.read_csv(..., dtype=str, keep_default_na=False)` — Apollo free-text fields have embedded commas/newlines.
- API keys via repo-root `.env` (loaded by `lib/config.py`). Vars: `PROSPEO_API_KEY`, `MV_API_KEY`, `ICY_API_KEY`, `ICY_API_SECRET`, `ICY_USER_ID`, `ADYNTEL_API_KEY`, `ADYNTEL_EMAIL`, `APIFY_API_TOKEN`, `APIFY_SIMILARWEB_ACTOR_ID`.
- Don't reformat Apollo input filenames — copy paths exactly from `os.listdir`.
- Adyntel returns HTTP 204 + empty body when a company has no ads — treat as `count=0`, not error.
- Apify `pro100chok/similarweb-scraper` requires min 10, max 50 domains per run. The earlier `crawlerbros/similarweb-scraper` got 403-blocked by SimilarWeb — abandoned.
