# M&A Deal Origination Pipeline — How It Works

> UK MSP Roll-Up · Thesis-in → Scored Target List out

---

## The Big Picture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ACQUISITION THESIS                           │
│           "UK IT MSPs · 10-50 employees · Founder-owned            │
│                  Owner 55+ · No PE backing · 10yr+"                │
│                       [ config/thesis.yaml ]                       │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 1 · SOURCING                              [ companies_house.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Companies House Public Data API (free)
  
  ┌──────────────┐    SIC codes          ┌──────────────────────────┐
  │  thesis.yaml │──► 62020, 62030   ──► │  /advanced-search/       │
  │              │    62090, 62012        │   companies              │
  │  6 regions   │──► West Midlands  ──► │                          │
  │              │    North West, etc.    │  + incorporated_before   │
  └──────────────┘                       │    (10+ years old only)  │
                                         └────────────┬─────────────┘
                                                      │
                                              ~200 raw companies
                                                      │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 2 · OWNERSHIP RESOLUTION    ◄── THE KEY SIGNAL [ psc.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PSC Register = Persons with Significant Control
  (Who actually owns and controls this company?)

  For each company:
  ┌─────────────────────────────────────────────────────────────┐
  │  /company/{number}/persons-with-significant-control        │
  └──────────┬──────────────────────────┬───────────────────────┘
             │                          │
    PSC = Individual              PSC = Corporate Entity
    (founder-owned ✓)             (part of a group ✗)
             │                          │
    Keep + capture:               ──► DROP (disqualified)
    · Owner name                  
    · Control % (75-100%)        Also capture from officers:
    · Age (from DoB)             · Director tenure
    · Nationality                · Age backfill if PSC missing DoB
             │
    succession_signal():
    owner.age >= 55 AND years_in_business >= 10
             │
    ~170 founder-owned companies remain
             │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 3 · SIZE SIGNAL                           [ accounts.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Companies House filed accounts (free)
  Private companies don't publish revenue — so we use proxies:

  ┌─────────────────────────────────────────────────────────────┐
  │  /company/{number}/accounts                                │
  └──────────┬──────────────────────────────────────────────────┘
             │
  accounts_category maps to revenue band:
  · micro-entity  →  < £632k turnover
  · small         →  < £10.2M turnover     ◄── our target range
  · full/medium   →  > £10.2M (too big)
  · dormant       →  disqualify
             │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 4 · WEBSITE RESOLUTION                      [ run.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Companies House doesn't return websites — so we derive them:

  "ALDERSTROM LIMITED"
         │
         ▼
  Strip legal suffix → "alderstrom"
         │
         ▼
  Try https://alderstrom.co.uk  ──► HEAD request ──► 200 OK? → use it
         │                                               │
         └── Try https://alderstrom.com ────────────────┘
         │
  If found → scrape visible text (strip nav/footer/scripts)
           → first 3,000 chars fed to LLM
         │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 5 · LLM CLASSIFICATION          [ classify_llm.py + Groq ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Model: llama-3.1-8b-instant via Groq (free tier, 30 req/min)
  2 prompts per company, run sequentially with rate throttling

  ┌────────────────────────────┐   ┌──────────────────────────────┐
  │   PROMPT 1: MSP CHECK      │   │   PROMPT 2: RECURRING REV    │
  │                            │   │                              │
  │  Is this a genuine MSP?    │   │  Does the site show          │
  │  (not a break-fix shop,    │   │  recurring-contract signals? │
  │   not a pure dev agency,   │   │  "managed", "monthly",       │
  │   not a VAR)               │   │  "SLA", "support plan"       │
  │                            │   │                              │
  │  → label: msp /            │   │  → confidence: 0-100         │
  │           adjacent /       │   │  → guess: recurring /        │
  │           unclear          │   │           mixed /            │
  │  → confidence: 0-100       │   │           project-based      │
  └────────────────────────────┘   └──────────────────────────────┘
              │                                  │
              └──────────────┬───────────────────┘
                             │
                    Both results stored per company
                             │
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 6 · FIT SCORING                          [ fit_score.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Weighted scoring against thesis.yaml — 0 to 100

  ┌─────────────────────────────────────────────────────────────────┐
  │  Component            Weight   Signal source                   │
  │  ─────────────────    ──────   ─────────────────────────────── │
  │  Industry match         20     Groq MSP classifier             │
  │  Size band (10-50 emp)  20     Accounts category / Apollo      │
  │  Founder-owned          20     PSC individual + no funding      │
  │  Succession signal      20     Owner age 55+ AND 10+ yrs biz   │
  │  Standalone (not group) 10     PSC not corporate entity         │
  │  Recurring revenue      10     Groq recurring-rev classifier   │
  │  ─────────────────    ──────                                   │
  │  TOTAL                 100                                     │
  └──────────────────────────────┬──────────────────────────────────┘
                                 │
              Hard disqualifiers (score = 0, regardless):
              · PSC is a company (group-owned)
              · Has funding rounds (PE/VC-backed)
              · Under 5 employees (lifestyle shop)
              · Company not active
                                 │
                    ┌────────────┴─────────────┐
                    │                          │
               Score ≥ 80               Score 60-79
               Tier A  🟢               Tier B  🟡
               Top targets              Good targets
                    │                          │
                    └────────────┬─────────────┘
                                 │   Score < 60
                                 │   Tier C  🔴  (drop / nurture)
                          ▼
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LAYER 7 · OUTPUT                          [ export.py + run.py ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Sorted by fit_score descending → data/final/msp_targets_YYYYMMDD.csv

  ┌────────────────────────────────────────────────────────────────┐
  │  company_name  ·  company_number  ·  region  ·  sic_codes     │
  │  incorporation_date  ·  years_in_business  ·  accounts_size   │
  │  owner_name  ·  owner_age  ·  owner_control_%  ·  nationality │
  │  succession_signal  ·  msp_label  ·  msp_confidence           │
  │  recurring_rev_confidence  ·  website_url                     │
  │  fit_score  ·  tier  ·  score_breakdown                       │
  │  owner_email  ·  email_verified  [when email enrichment on]   │
  └────────────────────────────────────────────────────────────────┘
                                 │
                   ┌─────────────┴──────────────┐
                   │                            │
           Load into Clay                Outreach via
           for sequencing            Smartlead / Instantly
                   │                    (templates in
           Review A/B targets       docs/outreach_variants.md)
           Polish top 20-30
```

---

## What Makes This Different from a Clay-Only Build

```
  Clay-only approach                This repo
  ──────────────────                ──────────────────────────────
  No PSC register access      vs.   PSC = who actually controls it
  Guesses ownership               Owner name, age, control % direct
                                    from the register

  Burns credits on LLM rows   vs.   LLM runs once locally per company
  (expensive at scale)              Groq = free, fast, unlimited

  Flat list of companies       vs.  Weighted 0-100 score per thesis
  No structured scoring             A/B/C tier, re-runnable per client

  Manual deduplication         vs.  Automated on company_number + domain

  Fixed verticals              vs.  Swap thesis.yaml → new client,
                                    new vertical, same pipeline
```

---

## Data Flow Summary

```
  thesis.yaml
      │
      ├──► Companies House API ──► ~200 candidates
      │         (SIC + region)
      │
      ├──► PSC Register ──────► drop corporate-owned ──► ~170 remain
      │    (owner identity)         (~15% disqualified)
      │
      ├──► Filed Accounts ────► revenue band proxy
      │    (size signal)
      │
      ├──► Domain guessing ───► website text (where available)
      │    + scraping
      │
      ├──► Groq LLM ──────────► msp_label + recurring_rev_confidence
      │    (2 prompts/company)
      │
      ├──► Fit Scorer ─────────► 0-100 score + A/B/C tier
      │    (thesis weights)
      │
      └──► CSV export ─────────► ranked target list
               │
               └──► [optional] Email waterfall
                    Prospeo → Icypeas → MillionVerifier
```

---

## Run Commands

```bash
# Fast demo (CH + PSC + scoring only, no LLM)
python run.py --max 200 --skip-llm --no-email

# Full pipeline
python run.py --max 200 --no-email

# Full pipeline with email enrichment
python run.py --max 200

# Single region test
python run.py --region "North West" --max 50 --no-email
```

---

*Built with: Companies House API · Groq (llama-3.1-8b-instant) · Python*  
*Cost to run 200 companies end-to-end: £0*
