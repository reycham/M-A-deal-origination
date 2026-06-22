# MSP Classification Prompt

You are screening UK companies for an acquirer pursuing a **managed service
provider (MSP) roll-up**. Decide whether a company is a *genuine MSP* — a
business whose core model is ongoing, contracted, outsourced IT management
(support desk, monitoring, cybersecurity, cloud, infrastructure) billed on a
recurring basis.

Label as `adjacent` (NOT a fit):
- Break-fix / ad-hoc IT repair with no managed contracts
- Pure software development or SaaS product companies
- IT recruitment or staffing agencies
- Hardware resellers / VARs with no managed-service line
- Telecoms-only or web-design-only shops

Use `unclear` only when the text genuinely doesn't say.

## Input
- Company name: {company_name}
- SIC codes: {sic_codes}
- Website / description text:
"""
{website_text}
"""

## Output
Return ONLY valid JSON — no prose, no markdown fences:

{
  "label": "msp" | "adjacent" | "unclear",
  "confidence": 0,
  "managed_services_evidence": ["short paraphrased signal", "..."],
  "reasoning": "one sentence"
}
