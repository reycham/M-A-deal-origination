# M&A Deal Origination Pipeline

> UK MSP Roll-Up · Thesis-in → Scored Target List out · Cost to run: £0

A thesis-driven deal origination pipeline that takes an acquisition criteria ("the box") as input and outputs a ranked, owner-identified list of private company targets. Built to mirror how a buy-side M&A advisor would source and qualify acquisition candidates — but automated end-to-end in Python.

---

## What It Does

Given a thesis like *"UK IT managed service providers, 10–50 employees, founder-owned, owner near retirement, no PE backing"*, the pipeline:

1. **Sources** every matching private company from Companies House (free, UK-wide)
2. **Identifies the real owner** via the PSC register — who actually controls it, their age, their control %
3. **Flags succession situations** — owner 55+, business 10+ years old
4. **Drops group-owned companies** — if the top PSC is a corporate entity, it's part of a group and not acquirable
5. **Classifies** each company as a genuine MSP or adjacent using an LLM reading the company's website
6. **Scores** every target 0–100 against the thesis weights and tiers them A/B/C
7. **Exports** a ranked CSV ready to load into Clay or Smartlead

---

## Why PSC Data Is the Edge

Most deal sourcing tools stop at company name + SIC code. This pipeline goes one step further: the Companies House **PSC (Persons with Significant Control) register** tells you who actually owns and controls a private company — the founder's name, their control percentage, and their date of birth. That's the succession signal that no generic Clay-only build surfaces.

---

## Pipeline Architecture

```
thesis.yaml
    │
    ├── Companies House API    →  raw universe (~200 companies)
    ├── PSC Register           →  owner identity + drop corporate-owned
    ├── Filed Accounts         →  revenue band proxy
    ├── Domain guessing        →  website URL resolution
    ├── Website scraping       →  text for LLM context
    ├── Groq LLM               →  MSP label + recurring-rev confidence
    ├── Fit Scorer             →  0-100 score + A/B/C tier
    └── CSV Export             →  ranked target list
```

Full visual flow: [`docs/pipeline-flow.md`](docs/pipeline-flow.md)

---

## Scoring Schema

| Component | Weight | Signal |
|-----------|--------|--------|
| Industry match (genuine MSP) | 20 | LLM classifier |
| Size band (10–50 employees) | 20 | Accounts category |
| Founder-owned (PSC individual, no funding) | 20 | PSC register |
| Succession signal (owner 55+, biz 10+ yrs) | 20 | PSC date of birth |
| Standalone (not a group subsidiary) | 10 | PSC type |
| Recurring revenue confidence | 10 | LLM classifier |

**Tiers:** A ≥ 80 · B 60–79 · C < 60

Hard disqualifiers (score = 0): corporate PSC, has funding rounds, under 5 employees, not active.

---

## Results

Running against 6 UK regions (West Midlands, East Midlands, North West, Yorkshire, North East, South West):

- **180** founder-owned companies identified
- **16** dropped (corporate-owned / group subsidiaries)  
- **71** B-tier targets (score 60–79)
- Top target: ETECHINC LIMITED — score 74, owner Mr Ilyas Ismail Jogiyat

---

## Project Structure

```
├── run.py                  # Entry point — orchestrates the full pipeline
├── config/
│   └── thesis.yaml         # Acquisition box — swap this per client
├── src/
│   ├── companies_house.py  # Companies House API — SIC + region search
│   ├── psc.py              # PSC register — owner identity + succession signal
│   ├── accounts.py         # Filed accounts — revenue band proxy
│   ├── apollo.py           # Apollo enrichment — employee count (optional)
│   ├── classify_llm.py     # Groq LLM — MSP + recurring-rev classification
│   ├── enrich_email.py     # Email waterfall — Prospeo → Icypeas → MillionVerifier
│   ├── fit_score.py        # Weighted scorer — 0-100 + A/B/C tier
│   ├── merge_dedupe.py     # Deduplication on company_number / domain
│   └── export.py           # CSV export
├── prompts/
│   ├── classify_msp.md     # LLM prompt: is this a genuine MSP?
│   └── recurring_rev.md    # LLM prompt: does it have recurring revenue?
├── docs/
│   ├── pipeline-flow.md    # Visual walkthrough of every pipeline layer
│   ├── outreach_variants.md # 4 email/LinkedIn templates for owner outreach
│   └── msp-deal-origination-spec.md  # Full thesis spec + scoring schema
└── data/
    └── final/              # Scored CSV output (gitignored)
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
# Fill in: COMPANIES_HOUSE_API_KEY, GROQ_API_KEY
# Optional: PROSPEO_API_KEY, ICYPEAS_API_KEY, MILLIONVERIFIER_API_KEY

# 3. Run
python run.py --max 200 --no-email      # Full pipeline, skip email enrichment
python run.py --max 200                 # Full pipeline including email enrichment
python run.py --skip-llm --no-email     # Fast demo — CH + PSC + scoring only
python run.py --region "North West"     # Single region
```

---

## API Keys

| Key | Purpose | Cost |
|-----|---------|------|
| `COMPANIES_HOUSE_API_KEY` | Source companies + PSC data | Free |
| `GROQ_API_KEY` | LLM classification | Free (14,400 req/day) |
| `PROSPEO_API_KEY` | Owner email finder | Free tier: 75/mo |
| `ICYPEAS_API_KEY` | Email fallback | Free tier available |
| `MILLIONVERIFIER_API_KEY` | Email verification | ~$5 / 1000 checks |

---

## Adapting to a New Thesis

Edit `config/thesis.yaml` — change the SIC codes, regions, employee band, succession age threshold, and scoring weights. The rest of the pipeline adapts automatically. No code changes needed.

---

*Built with: Companies House Public Data API · Groq (llama-3.1-8b-instant) · Python*
