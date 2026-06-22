# Deal Origination Demo — UK MSP Roll-Up

A thesis-driven, Claude Code–orchestrated pipeline that takes an acquisition thesis as input and outputs a scored, owner-identified target list. Built to mirror ToplineX's "total market mapping → signals → enrichment" deliverable for their deal origination line.

---

## 1. The Thesis Box (example)

This is the acquirer's acquisition criteria — the "box." In the repo it lives in `config/thesis.yaml` so the whole pipeline is reusable per client.

| Dimension | Target |
|---|---|
| **Industry** | UK IT Managed Service Providers (MSPs) / outsourced IT support |
| **SIC codes** | 62020 (IT consultancy), 62030 (computer facilities management), 62090 (other IT services), 62012 (business software dev), 95110 (computer repair) |
| **Geography** | England — bias to Midlands / North (less competed than London/SE) |
| **Size** | 10–50 employees → proxy for ~£1M–£6M revenue (MSPs run ~£80k–£150k revenue/head) |
| **EBITDA proxy** | ~£100k–£1.2M (10–20% MSP margins) — lower-middle-market search/PE box |
| **Ownership** | Founder/owner-managed, no institutional (PE/VC) backing |
| **Age** | 10+ years incorporated |
| **Recurring revenue** | Managed-contract model (inherent to MSP — that's why this vertical) |
| **Succession signal** | ≥1 controlling director aged 55+ |

**Hard disqualifiers (auto-zero, drop):**
- PSC is a company, not a person (→ part of a group / holding structure)
- Already PE/VC-backed (Crunchbase funding rounds present)
- < 5 employees (lifestyle / break-fix, too small)
- Company status not "active" (dormant, in liquidation, dissolved)

---

## 2. Signal Sources → which tool fills which field

| Field | Source |
|---|---|
| company_number, sic_codes, incorporation_date, registered region, company_status | **Companies House API** (free) |
| **owner identity + control %** | **Companies House PSC register** (the key signal) |
| owner age (month/year DoB) | **Companies House officers** |
| group vs standalone | Companies House PSC type (individual vs corporate) + accounts type |
| employee_count, description, industry | Grata / Apollo / Crunchbase |
| estimated_revenue / filed accounts | Companies House filing history (where available) + provider estimate |
| funding_status | Crunchbase (none = good) |
| owner LinkedIn, local corroboration | Apify (LinkedIn + Google Maps) |
| owner_email | Prospeo / Icypeas |
| email_status | MillionVerifier |
| "is it really an MSP?" + recurring-rev inference | **Claude (LLM classification)** on website/description |

---

## 3. Scoring Schema

### Clay table columns
```
company_name · company_number · website · domain · sic_codes ·
region · incorporation_date · years_in_business · employee_count ·
estimated_revenue · funding_status · psc_type · psc_name · psc_control_pct ·
owner_name · owner_role · owner_age · owner_linkedin · owner_email ·
email_status · recurring_rev_confidence · company_status · is_subsidiary ·
description · fit_score · score_breakdown · tier
```

### Fit score (out of 100)
| Component | Weight | Logic |
|---|---|---|
| Industry / SIC match | 20 | LLM confirms it's a genuine MSP, not an adjacent IT firm |
| Size in band (10–50 emp) | 20 | Full marks in band, partial 5–10 or 50–75, zero outside |
| Founder-owned (PSC = individual, no funding) | 20 | PSC individual with ≥25% control AND no Crunchbase funding |
| Succession signal (owner 55+ AND 10+ yrs) | 20 | Both conditions → 20; one → 10 |
| Standalone (not subsidiary/group) | 10 | PSC not corporate, no group accounts |
| Recurring-revenue confidence | 10 | LLM read of site: "managed/contract/MRR" language |

**Tiers:** A ≥ 80 · B 60–79 · C < 60 (drop or long-term nurture).
Any hard disqualifier → score = 0 regardless of components.

> Deal origination lists are small and high-conviction — hundreds of A/B targets, not thousands. Quality per row beats reach.

---

## 4. Claude Code Repo Structure

> **Layout note:** the files are kept **flat** in the project root (not nested in
> `src/`) so imports stay simple — `psc.py` does `from companies_house import _get`.
> Owner-age logic lives **inside `psc.py`** (no separate `officers.py`). If you
> later prefer a nested `src/` tree, add `__init__.py` files and update imports to
> full paths.

```
msp-deal-origination/
├── README.md
├── .env.example              # companies_house, apollo, crunchbase, prospeo,
│                             # millionverifier, anthropic keys
├── config/
│   └── thesis.yaml           # the acquisition box (swap this per client)
├── data/
│   ├── raw/                  # raw pulls per source
│   ├── interim/              # merged + deduped
│   └── final/                # scored target list (csv)
├── prompts/
│   ├── classify_msp.md       # is-it-really-an-MSP classifier
│   └── recurring_rev.md      # recurring-revenue inference from site copy
├── companies_house.py        # [DONE] advanced search by SIC + region → company list
├── psc.py                    # [DONE] PSC register → owner + control % + age (key)
├── apollo.py                 # employee count, description, est. revenue
├── crunchbase.py             # funding status (none = founder-owned)
├── maps_apify.py             # local corroboration + owner LinkedIn
├── merge_dedupe.py           # consolidate sources, dedupe on company_number / domain
├── accounts.py               # Companies House filing history → size read
├── email.py                  # prospeo/icypeas → millionverifier
├── classify_llm.py           # runs prompts/ via Claude (MSP + recurring-rev)
├── fit_score.py              # applies thesis.yaml weights → 0-100 + A/B/C tier
├── export.py                 # to_csv / push to Clay or Airtable
└── run.py                    # orchestrates: source → consolidate → enrich → score → export
```

### `config/thesis.yaml` (example)
```yaml
industry: UK Managed Service Provider
sic_codes: [62020, 62030, 62090, 62012, 95110]
regions: [West Midlands, East Midlands, North West, Yorkshire]
employee_band: { min: 10, max: 50 }
min_years_in_business: 10
ownership: founder_owned          # PSC must be an individual
max_funding_rounds: 0
succession:
  min_owner_age: 55
weights:
  industry_match: 20
  size_band: 20
  founder_owned: 20
  succession: 20
  standalone: 10
  recurring_rev: 10
disqualifiers: [psc_is_company, has_funding, under_5_employees, not_active]
```

### Why Claude Code here, not pure Clay
The LLM classification steps (genuine-MSP check, recurring-revenue inference) and the PSC parsing are where Clay credits burn fast and break. Orchestrating it as a repo means: thesis in → scored list out, re-runnable per client, and the expensive reasoning runs once locally instead of per-row in Clay. That's the exact "Claude Code + Clay" workflow ToplineX positions on — and the part a no-code applicant can't replicate.

---

## 5. Build order (suggested)
1. ✅ `companies_house.py` — search by SIC + region, get the raw universe. Free, no paid tools. **(built)**
2. ✅ `psc.py` — owner identity, control %, age (officer-age backfill included). This alone is a demo-worthy slice. **(built)**
3. `classify_llm.py` + `prompts/` — filter to genuine MSPs, infer recurring revenue.
4. `email.py` — owner email + verification (Prospeo → Icypeas → MillionVerifier).
5. `fit_score.py` — apply `thesis.yaml` weights, tier the list A/B/C.
6. `export.py` → Clay/Airtable, then record a Loom leading with the PSC logic.

> Minimum credible demo = steps 1, 2, 5, 6. Steps 3–4 make it stronger but aren't make-or-break.

---

## 6. What to hand the founder
- Live Clay table (or sanitized CSV export) of scored A/B targets
- The repo (public GitHub, README explaining thesis-in → list-out)
- One-page thesis-to-targets writeup
- 3-minute Loom — open on the PSC + succession-signal logic, not the tooling
