# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this repo is

A multi-project enrichment workspace. Each project under `projects/<name>/` is a self-contained data-matching + email-enrichment pipeline. Shared helpers live in `lib/`. API keys live in `.env` at repo root.

The first project, `projects/uk_smb_4icp/`, is complete (1,862 deliverable contacts across 4 UK SMB ICPs as of 2026-05-03, with Adyntel + SimilarWeb signals layered on top). New enrichment work should drop into a new `projects/<name>/` folder rather than adding files at the root.

## Layout

```
.env                         # secrets (gitignored)
.env.example                 # committed template
lib/
  config.py                  # load_dotenv + get_key("VAR")
projects/
  uk_smb_4icp/
    inputs/  scripts/  output/  README.md
```

See `projects/uk_smb_4icp/README.md` for the working pipeline. Four stacked layers:

1. **Email layer** (scripts 01–06): `match_dms` → `enrich_emails` → `generate_email_patterns` → `merge_verified` → `finalize` → `icypeas_pipeline`. Output: `output/final_v2/<ICP> - <persona> final.csv`.
2. **Signals layer** (scripts 07–10): `build_signals_input` → `enrich_adyntel` → `enrich_similarweb` → `merge_signals`. Output: `output/final_v3/<ICP> - <persona> with signals.csv`.
3. **Scoring layer** (script 11): `score_leads`. Output: `output/final_v4/<ICP> - <persona> scored.csv` — adds `lead_score` (0–100) + `tier` (Hot/Warm/Cool/Cold for deliverable, X for non-deliverable).
4. **Website-summary layer** (script 12): `enrich_website_summary`. **Canonical output: `output/final_v5/<ICP> - <persona> with summary.csv`** — adds `website_summary` (Haiku-generated 2-3 sentence factual blurb from homepage + about page) for ~76% of companies. Plus `_all_deliverable_ranked.csv` (1,862 rows sorted by score with all signals + summary).

Outreach filters:
- For email cadences: `tier in ('Hot','Warm','Cool','Cold')` (i.e. excludes X).
- Hot tier (198 contacts) = top 11% — work first. Tunable in the `WEIGHTS` dict at top of `11_score_leads.py`.

## Vendor APIs (env vars)

| Vendor | Vars | Notes |
|---|---|---|
| Prospeo | `PROSPEO_API_KEY` | `X-KEY` header, `enrich-person`, STARTER 2,400/mo |
| MillionVerifier | `MV_API_KEY` | `key` query param, bulk endpoints |
| Icypeas | `ICY_API_KEY`, `ICY_API_SECRET`, `ICY_USER_ID` | HMAC-SHA1: `Authorization: <apiKey>:<sig>`, `X-ROCK-TIMESTAMP: <ISO 8601 with .ms>`, `sig = hmac_sha1(secret, lower(METHOD + path + timestamp))`. Result fetch via `/api/bulk-single-searchs/read` with `mode: bulk, file: <file_id>` paginates 100/page. Working signing code: `projects/uk_smb_4icp/scripts/icypeas_pipeline.py`. |
| Adyntel | `ADYNTEL_API_KEY`, `ADYNTEL_EMAIL` | Auths via JSON body (not header): `{email, api_key, company_domain}`. `POST https://api.adyntel.com/facebook` returns `number_of_ads`; `POST .../google` returns `total_ad_count`. **HTTP 204 + empty body = "no ads found", treat as count=0**. 1 credit per successful call, ~3,400 calls cost ~$33. |
| Apify (SimilarWeb) | `APIFY_API_TOKEN`, `APIFY_SIMILARWEB_ACTOR_ID=aqPbs3KeH9aD8b22w` | Actor `pro100chok/similarweb-scraper`. **Min 10, max 50 domains per actor run.** Input: `{searchType: "similarweb", domains: [...]}`. Pricing $2/1000 results. Use sequential batches with concurrent runs (5 workers) — completed 1,670 domains in ~5 min for $3.34. The earlier `crawlerbros/similarweb-scraper` actor got 403-blocked by SimilarWeb — avoid. |
| Firecrawl | `FIRECRAWL_API_KEY` | `POST https://api.firecrawl.dev/v1/scrape` with `{url, formats: ["markdown"], onlyMainContent: true}`. Header `Authorization: Bearer <key>`. ~$0.0015/page. Used in script 12 for homepage + about-page scraping. |
| Anthropic (Haiku 4.5) | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001`. **Rate limit: 50K input tokens/min** — keep concurrency ≤12 workers when sending ~2K tokens/call. Use prompt caching (`cache_control: {type: "ephemeral"}` on system prompt) for repeated runs. ~$0.003/call. **Note**: prompts asking for a single-string sentinel (e.g. "output `INSUFFICIENT_CONTENT`") will frequently leak an explanation — strip post-hoc with substring match. |

## Working conventions

- No build system / tests. Tasks are ad-hoc Python scripts.
- API keys in `.env` only — never commit. Load via `from lib.config import get_key`.
- Read CSVs with `pandas.read_csv(..., dtype=str, keep_default_na=False)` — vendor exports contain embedded commas/newlines.
- Preserve original input CSVs; write outputs under each project's `output/`.
- Apollo input filenames have inconsistent spacing — copy paths exactly from `os.listdir`, don't reformat.
- Refactor shared API clients into `lib/` only when a second project needs them. Don't preemptively abstract the existing `uk_smb_4icp` scripts — they're done and run.
- Windows shell is PowerShell; the Bash tool is also available.

## Adding a new enrichment project

1. `mkdir projects/<name>/{inputs,scripts,output}` and add a `README.md` describing brief + pipeline.
2. Reuse `lib.config.get_key(...)` for env access; add new env vars to `.env.example`.
3. If you copy a vendor client out of `uk_smb_4icp/scripts/`, that's the trigger to extract it into `lib/<vendor>.py` and update both call sites.
