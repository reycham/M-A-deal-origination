# After5 Digital — GTM Automation System

A production outbound GTM pipeline built end-to-end with **Claude Code** — from raw Apollo exports to live SmartLead campaigns. Solo-built in ~2 weeks.

---

## What this does

Takes 4 UK SMB ICP company lists → enriches contacts → layers in buying-intent signals → scores every lead → generates personalised openers → pushes campaigns to SmartLead.

**Output: 1,862 deliverable contacts across 4 ICPs, each with a verified email, lead score, and personalised first liner. 198 classified as Hot (running ads + real traffic + senior DM).**

---

## Full pipeline (19 scripts, ~$40 in API costs)

### Layer 0 — Infrastructure (manual)
- Bought domains and warmed email inboxes (Google Workspace + SmartLead warm-up)
- Defined 4 ICPs using AI Ark for market sizing signals
- Pulled contact lists from Apollo (~26,000 raw records across 8 scrapes)

### Layer 1 — Contact matching & email enrichment (`scripts/01–06`)

| Script | What it does |
|---|---|
| `match_dms.py` | 3-pass join (LinkedIn URL → domain → name) across 8 Apollo exports; picks highest-authority decision-maker per company |
| `enrich_emails.py` | Prospeo `enrich-person` for contacts without emails (LinkedIn-keyed, resumable) |
| `generate_email_patterns.py` | Generates 6 candidate `{first}.{last}@domain` patterns for remaining gaps |
| `merge_verified.py` | Merges MillionVerifier bulk results (`ok` / `catch_all`) |
| `finalize.py` | Consolidates Apollo + Prospeo + MV into clean per-ICP/persona CSVs |
| `icypeas_pipeline.py` | HMAC-SHA1 signed bulk search + verification via Icypeas; phase 2 verifies all emails |

**Result: 1,862 deliverable contacts (down from ~26K raw — most were already in Apollo with verified emails)**

### Layer 2 — Buying-intent signals (`scripts/07–10`)

| Script | What it does |
|---|---|
| `07_build_signals_input.py` | Deduplicates to 1,691 unique companies for enrichment |
| `08_enrich_adyntel.py` | Per-company Meta + Google ad counts via Adyntel (~3,400 calls, ~$33, 8–20 concurrent workers, resumable) |
| `09_enrich_similarweb.py` | Bulk SimilarWeb traffic via Apify actor `pro100chok/similarweb-scraper` — batches of 50 domains, 5 concurrent runs, 1,670 domains in ~5 min for $3.34 |
| `10_merge_signals.py` | Left-joins Adyntel (on LinkedIn URL) + SimilarWeb (on domain) into enriched contact list |

### Layer 3 — Lead scoring (`script/11`)

Weighted 0–100 additive score across 7 signal components:

| Component | Max pts | Logic |
|---|---:|---|
| Email certainty | 10 | `ultra_sure` → 10, `very_sure` → 7, `probable` → 4 |
| Paid-ad activity | 25 | Meta ads > 0 → +10, Google ads > 0 → +10, heavy spender → +5 |
| Traffic volume | 30 | Bucketed: <500 → 0, 500–2k → 8, 2k–10k → 18, 10k–50k → 25, 50k+ → 30 |
| Engagement | 10 | Bounce < 50% → +5, pages/visit > 2 → +5 |
| Geo (UK) | 5 | SimilarWeb `top_country == GB` |
| DM seniority | 10 | Founder/CEO/MD title → +10 |
| Revenue band | 10 | UK SMB sweet spot ($500K–5M) → +10 |

**Tier breakdown:** Hot ≥75 (198) · Warm 50–74 (879) · Cool 25–49 (736) · Cold <25 (49)

### Layer 4 — Website summaries (`script/12`)

Firecrawl scrapes homepage + `/about` page → Claude Haiku 4.5 generates a 2–3 sentence factual company blurb per contact. ~76% coverage. Used as personalisation context for openers.

- Rate-limited to ≤12 concurrent Haiku calls (50K token/min limit)
- Resumable: skips already-processed companies on re-run
- Cost: ~$0.003/call via `claude-haiku-4-5-20251001`

### Layer 5 — Personalised openers (`scripts/13–16`)

Signal-branched deterministic first liners — no LLM cost at scale:

```
both_ads       → "Came across {company} today and noticed you're running ads on both Meta and Google."
meta_only      → "Came across {company} today and noticed you're running ads on Meta."
google_only    → "Came across {company} today and noticed you're running ads on Google."
organic_traffic → "Came across {company} today and noticed you're pulling decent traffic through search."
fallback       → one of 3 generic variants (randomly seeded)
```

Company names stripped of legal suffixes (`Ltd`, `PLC`, `Holdings`, etc.) for natural copy.

### Layer 6 — SmartLead campaign push (`scripts/17–19`)

| Script | What it does |
|---|---|
| `17_build_smartlead_campaigns.py` | Parses email template markdown → creates 4 campaigns (one per ICP) via SmartLead REST API → adds 1,862 leads with custom variables → sets 3-step sequences (E1 → +3d → E2 → +4d → E3) |
| `18_fix_opener_in_smartlead.py` | Re-syncs personalised openers to existing campaigns if templates changed |
| `19_configure_campaigns.py` | Assigns mailboxes and schedules; leaves campaigns paused for final review before go-live |

---

## Repo layout

```
.env.example                 # all required API keys (copy → .env, fill in)
requirements.txt
lib/
  config.py                  # load_dotenv + get_key("VAR") helper
projects/
  uk_smb_4icp/               # ← the pipeline above; complete
    scripts/                 # 19 scripts in run order
    inputs/                  # gitignored (Apollo exports, MV report)
    output/                  # gitignored (all intermediate + final CSVs)
    README.md                # detailed pipeline notes + signal schema
    OUTBOUND_BRIEF.md        # brief written for copy team
  uk_cro_shopify/            # second project — Clutch scrape → agency list
    scripts/01_scrape_clutch.py
    scripts/02_clean_export.py
After5 Digital — SmartLead Email Templates with Spintax.md
```

---

## Vendor stack

| Vendor | Used for | Cost |
|---|---|---|
| Apollo | Contact list exports | Subscription |
| Prospeo | LinkedIn-keyed email enrichment | STARTER 2,400/mo |
| MillionVerifier | Bulk email verification | Bulk UI export |
| Icypeas | HMAC-SHA1 signed bulk search + verify | Per lookup |
| Adyntel | Meta + Google ad-spend signals | ~$33 / 3,400 calls |
| Apify (SimilarWeb) | Monthly traffic + engagement | ~$3.34 / 1,670 domains |
| Firecrawl | Homepage + about-page scraping | ~$0.0015/page |
| Claude Haiku 4.5 | Website summary generation | ~$0.003/company |
| SmartLead | Campaign sequencing + send | Subscription |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in API keys
```

All scripts run from the repo root: `python projects/uk_smb_4icp/scripts/<script>.py`

---

## Built with Claude Code

Every script in this repo was written conversationally with [Claude Code](https://claude.com/claude-code). The workflow: describe what layer needs building → Claude Code writes the script, handles edge cases (HTTP 204 = no ads, embedded newlines in CSVs, HMAC signing, resumable state), and wires it into the pipeline. Total human code written: ~0 lines.

This is what agentic-assisted GTM automation looks like in practice.
