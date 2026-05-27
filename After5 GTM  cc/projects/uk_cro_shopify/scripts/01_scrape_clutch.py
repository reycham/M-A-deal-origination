"""
Scrape UK CRO agencies (Shopify filter) from Clutch.co using Playwright.
Output: projects/uk_cro_shopify/output/raw_clutch.json

Run: python projects/uk_cro_shopify/scripts/01_scrape_clutch.py
"""

import json
import re
import sys
import time
import random
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_FILE = PROJECT_DIR / "output" / "raw_clutch.json"

# ── target — Clutch UK CRO listing with Shopify filter ───────────────────────
START_URL = (
    "https://clutch.co/uk/agencies/conversion-optimization"
    "?filter[services][0]=shopify"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── selectors (verified against live Clutch HTML 2026-05-10) ─────────────────
PROVIDER_ROW   = "li.provider-list-item"
NAME_SEL       = "a.provider__title-link"
RATING_SEL     = "span.sg-rating__number"
REVIEWS_SEL    = "a.sg-rating__reviews"
DESCRIPTION_SEL= "div.provider__description"
MIN_PROJECT_SEL= "div.min-project-size"
HOURLY_RATE_SEL= "div.hourly-rate"
EMPLOYEES_SEL  = "div.employees-count"
LOCATION_SEL   = "div.location"
WEBSITE_SEL    = "a.website-link__item"
SERVICES_SEL   = "span.provider__services-chart-item"


def rand_delay(lo=2.5, hi=5.0):
    time.sleep(lo + random.random() * (hi - lo))


def safe_text(el, selector: str) -> str:
    try:
        node = el.query_selector(selector)
        return node.inner_text().strip() if node else ""
    except Exception:
        return ""


def safe_attr(el, selector: str, attr: str) -> str:
    try:
        node = el.query_selector(selector)
        return (node.get_attribute(attr) or "").strip() if node else ""
    except Exception:
        return ""


def extract_website(el) -> str:
    """Clutch wraps outbound links in r.clutch.co/redirect?...&u=<real_url>.
    Extract the real destination from the 'u' query param."""
    raw = safe_attr(el, WEBSITE_SEL, "href")
    if not raw:
        return ""
    try:
        qs = parse_qs(urlparse(raw).query)
        if "u" in qs:
            return qs["u"][0]
    except Exception:
        pass
    return raw


def extract_services(el) -> list[str]:
    """Services are in data-tooltip-content as '<i>20% Conversion Optimization</i>'."""
    nodes = el.query_selector_all(SERVICES_SEL)
    services = []
    for node in nodes:
        tooltip = node.get_attribute("data-tooltip-content") or ""
        # strip HTML tags
        text = re.sub(r"<[^>]+>", "", tooltip).strip()
        if text:
            services.append(text)
    return services


def extract_agencies(page) -> list[dict]:
    rows = page.query_selector_all(PROVIDER_ROW)
    agencies = []
    for row in rows:
        name = safe_text(row, NAME_SEL)
        if not name:
            continue

        profile_url = safe_attr(row, NAME_SEL, "href")
        # ensure absolute
        if profile_url and not profile_url.startswith("http"):
            profile_url = "https://clutch.co" + profile_url

        agencies.append({
            "name":             name,
            "clutch_url":       profile_url,
            "website":          extract_website(row),
            "description":      safe_text(row, DESCRIPTION_SEL),
            "rating":           safe_text(row, RATING_SEL),
            "reviews_count":    safe_text(row, REVIEWS_SEL),
            "location":         safe_text(row, LOCATION_SEL),
            "min_project_size": safe_text(row, MIN_PROJECT_SEL),
            "hourly_rate":      safe_text(row, HOURLY_RATE_SEL),
            "employees":        safe_text(row, EMPLOYEES_SEL),
            "services":         extract_services(row),
        })
    return agencies


def get_next_page_url(current_url: str, page_num: int) -> str | None:
    """Build next page URL by appending &page=N to the original filter URL.
    Clutch's rel='next' link drops the Shopify filter param, so we construct manually."""
    base = START_URL.split("&page=")[0]  # strip any existing page param
    next_url = f"{base}&page={page_num + 1}"
    return next_url


def scrape() -> list[dict]:
    all_agencies: list[dict] = []
    seen: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
        )
        context.set_extra_http_headers({
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        page = context.new_page()

        url = START_URL
        page_num = 1

        while url:
            print(f"  Page {page_num}: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeout:
                print(f"  Timeout on page {page_num} — stopping.")
                break

            try:
                page.wait_for_selector(PROVIDER_ROW, timeout=15_000)
            except PlaywrightTimeout:
                print(f"  No agency cards on page {page_num} — end of results.")
                break

            # scroll to trigger any lazy-loaded cards
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.2)

            agencies = extract_agencies(page)
            new = 0
            for a in agencies:
                key = a["clutch_url"] or a["name"].lower()
                if key not in seen:
                    seen.add(key)
                    all_agencies.append(a)
                    new += 1

            print(f"  -> {new} new agencies (total: {len(all_agencies)})")

            if not agencies:
                print("  Zero agencies extracted — stopping.")
                break

            next_url = get_next_page_url(url, page_num)
            if not next_url or next_url == url:
                print("  No next page — done.")
                break

            url = next_url
            page_num += 1
            rand_delay()

        browser.close()

    return all_agencies


def main():
    if OUTPUT_FILE.exists():
        print(f"Output already exists: {OUTPUT_FILE}")
        print("Delete it to re-run.")
        sys.exit(0)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print("=== Clutch UK CRO Shopify Scraper ===")
    print(f"Start URL: {START_URL}\n")

    agencies = scrape()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(agencies, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(agencies)} agencies saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
