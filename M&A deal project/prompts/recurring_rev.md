# Recurring-Revenue Inference Prompt

Estimate how strongly a company's public copy indicates **recurring revenue**
(monthly/annual contracts, retainers, managed plans, subscriptions, SLAs)
versus one-off project work. Recurring revenue is what makes a target
acquirable at a healthy multiple, so this score feeds the fit model.

## Input
- Company name: {company_name}
- Website / description text:
"""
{website_text}
"""

## Signals FOR recurring revenue
"managed", "monthly", "per user / per seat", "support plan", "retainer",
"subscription", "SLA", "contract", "ongoing", "fully managed"

## Signals AGAINST
"project", "one-off", "quote", "bespoke build", "per-hour", "ad hoc", "as needed"

## Output
Return ONLY valid JSON — no prose, no markdown fences.
`recurring_rev_confidence` is an INTEGER from 0 to 100 (not a decimal, not a fraction).
Examples: 85 means strong recurring signals. 20 means mostly project-based. 50 means mixed or unclear.

{
  "recurring_rev_confidence": 50,
  "evidence": ["short paraphrased signal", "..."],
  "model_guess": "recurring" | "mixed" | "project-based"
}
