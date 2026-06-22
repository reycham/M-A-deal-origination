"""
classify_llm.py
Run the MSP classification and recurring-revenue prompts via Groq.

Reads prompts from classify_msp.md and recurring_rev.md (prompts/).
Falls back gracefully when no website URL is available — uses the company
name + SIC codes as the classification input.

Deps: pip install groq requests beautifulsoup4
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
_PROMPTS_DIR = _ROOT / "prompts"
_CLASSIFY_MSP_PATH = _PROMPTS_DIR / "classify_msp.md"
_RECURRING_REV_PATH = _PROMPTS_DIR / "recurring_rev.md"

_CLASSIFY_PROMPT: str = _CLASSIFY_MSP_PATH.read_text(encoding="utf-8")
_RECURRING_PROMPT: str = _RECURRING_REV_PATH.read_text(encoding="utf-8")

MODEL = "llama-3.1-8b-instant"
MAX_WEBSITE_CHARS = 3000  # trim to keep tokens low

# Groq free tier: 30 req/min. Sleep 2s between calls to stay comfortably under.
_RATE_LIMIT_SLEEP = 2.0
_last_call_time = 0.0


def _throttle() -> None:
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < _RATE_LIMIT_SLEEP:
        time.sleep(_RATE_LIMIT_SLEEP - elapsed)
    _last_call_time = time.time()


def _get_client():
    try:
        from groq import Groq
    except ImportError:
        raise ImportError("pip install groq")
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY in your environment / .env")
    return Groq(api_key=api_key)


def fetch_website_text(url: str, timeout: int = 10) -> str:
    """Scrape visible text from a URL. Returns empty string on failure."""
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError("pip install beautifulsoup4")

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; deal-research-bot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:MAX_WEBSITE_CHARS]
    except Exception as exc:
        logger.debug("Website fetch failed for %s: %s", url, exc)
        return ""


def _call_llm(prompt: str) -> dict:
    _throttle()
    client = _get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a business analyst. Always respond with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    return json.loads(resp.choices[0].message.content)


def classify_msp(
    company_name: str,
    sic_codes: list[str],
    website_text: str,
) -> dict:
    """
    Returns: {label, confidence, managed_services_evidence, reasoning}
    label: "msp" | "adjacent" | "unclear"
    confidence: 0–100
    """
    text_block = website_text or f"(no website text available — classify from name and SIC codes only)"
    prompt = (
        _CLASSIFY_PROMPT
        .replace("{company_name}", company_name)
        .replace("{sic_codes}", ", ".join(sic_codes))
        .replace("{website_text}", text_block)
    )
    try:
        return _call_llm(prompt)
    except Exception as exc:
        logger.warning("MSP classify failed for %s: %s", company_name, exc)
        return {"label": "unclear", "confidence": 0, "managed_services_evidence": [], "reasoning": str(exc)}


def classify_recurring_rev(
    company_name: str,
    website_text: str,
) -> dict:
    """
    Returns: {recurring_rev_confidence, evidence, model_guess}
    recurring_rev_confidence: 0–100
    model_guess: "recurring" | "mixed" | "project-based"
    """
    text_block = website_text or "(no website text available)"
    prompt = (
        _RECURRING_PROMPT
        .replace("{company_name}", company_name)
        .replace("{website_text}", text_block)
    )
    try:
        return _call_llm(prompt)
    except Exception as exc:
        logger.warning("Recurring-rev classify failed for %s: %s", company_name, exc)
        return {"recurring_rev_confidence": 0, "evidence": [], "model_guess": "unclear"}


def classify_company(
    company_name: str,
    sic_codes: list[str],
    website_url: str = "",
    description: str = "",
) -> dict:
    """
    Full classification: fetch website, run both prompts.
    Returns merged result dict.
    """
    website_text = fetch_website_text(website_url) if website_url else description
    msp = classify_msp(company_name, sic_codes, website_text)
    rec = classify_recurring_rev(company_name, website_text)
    return {
        "msp_label": msp.get("label", "unclear"),
        "msp_confidence": msp.get("confidence", 0),
        "msp_evidence": msp.get("managed_services_evidence", []),
        "msp_reasoning": msp.get("reasoning", ""),
        "recurring_rev_confidence": rec.get("recurring_rev_confidence", 0),
        "recurring_rev_guess": rec.get("model_guess", "unclear"),
        "recurring_rev_evidence": rec.get("evidence", []),
    }


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    name = sys.argv[1] if len(sys.argv) > 1 else "Acme IT Solutions Ltd"
    url = sys.argv[2] if len(sys.argv) > 2 else ""
    result = classify_company(name, ["62020", "62030"], website_url=url)
    print(json.dumps(result, indent=2))
