"""Wire mailboxes + schedule + settings into the 4 Smartlead campaigns.

Allocation (33 ready after5 mailboxes, share-weighted by lead count):
  Real estate (730 leads): 13 mailboxes
  Recruitment (479 leads):  9 mailboxes
  Mortgage    (399 leads):  7 mailboxes
  Dealership  (223 leads):  4 mailboxes

Schedule: Mon-Fri 09:00-17:00 Europe/London
Settings: open/click tracking OFF, stop on reply, plain text OFF.

Default mode is dry-run; pass --push to apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.config import get_key  # noqa: E402

OUT_DIR = PROJECT_ROOT / "output" / "smartlead"
SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"

# Allocation: ICP -> number of mailboxes (must sum to 33).
ALLOCATION = [
    ("Real estate", 13, 80),
    ("Recruitment",  9, 55),
    ("Mortgage",     7, 45),
    ("Dealership",   4, 25),
]

SCHEDULE_BASE = {
    "timezone": "Europe/London",
    "days_of_the_week": [1, 2, 3, 4, 5],   # Mon-Fri
    "start_hour": "09:00",
    "end_hour": "17:00",
    "min_time_btw_emails": 10,              # minutes between sends from same mailbox
    # max_new_leads_per_day is filled per-campaign from ALLOCATION
}

SETTINGS = {
    "track_settings": ["DONT_TRACK_EMAIL_OPEN", "DONT_TRACK_LINK_CLICK"],
    "stop_lead_settings": "REPLY_TO_AN_EMAIL",
    "send_as_plain_text": True,
}


def fetch_after5_ready_mailboxes(api_key: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_BASE}/email-accounts/?api_key={api_key}&offset={offset}&limit=100",
            timeout=30)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
        offset += 100
    after5 = [
        a for a in out
        if a.get("is_smtp_success")
        and ("after5" in (a.get("from_email") or "").lower()
             or "aftr5"  in (a.get("from_email") or "").lower())
    ]
    after5.sort(key=lambda a: -int(a["id"]))  # newest first
    return after5


def assign(mailboxes: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    i = 0
    for icp, n, max_new in ALLOCATION:
        out[icp] = {"mailboxes": mailboxes[i:i + n], "max_new_leads_per_day": max_new}
        i += n
    return out


def push_emails(api_key: str, cid: int, mailbox_ids: list[int]) -> None:
    r = requests.post(
        f"{SMARTLEAD_BASE}/campaigns/{cid}/email-accounts?api_key={api_key}",
        json={"email_account_ids": mailbox_ids}, timeout=30)
    r.raise_for_status()


def push_schedule(api_key: str, cid: int, max_new_leads_per_day: int) -> None:
    body = {**SCHEDULE_BASE, "max_new_leads_per_day": max_new_leads_per_day}
    r = requests.post(
        f"{SMARTLEAD_BASE}/campaigns/{cid}/schedule?api_key={api_key}",
        json=body, timeout=30)
    r.raise_for_status()


def push_settings(api_key: str, cid: int) -> None:
    # Smartlead /settings endpoint is POST (PATCH returns 404).
    r = requests.post(
        f"{SMARTLEAD_BASE}/campaigns/{cid}/settings?api_key={api_key}",
        json=SETTINGS, timeout=30)
    r.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    api_key = get_key("SMARTLEAD_API_KEY")

    ids_path = OUT_DIR / "_campaign_ids.json"
    campaign_ids: dict[str, int] = json.loads(ids_path.read_text())
    print(f"Loaded campaign ids: {campaign_ids}\n")

    mailboxes = fetch_after5_ready_mailboxes(api_key)
    if len(mailboxes) < 33:
        sys.exit(f"Only {len(mailboxes)} ready after5 mailboxes; need 33.")
    mailboxes = mailboxes[:33]
    print(f"Using top 33 of {len(mailboxes)} ready after5 mailboxes (newest first).\n")

    plan = assign(mailboxes)
    for icp, group in plan.items():
        print(f"=== {icp}  ({len(group['mailboxes'])} mailboxes, max_new_leads_per_day={group['max_new_leads_per_day']}) ===")
        for m in group["mailboxes"]:
            print(f"  {m['id']:>9}  {m['from_email']}")
        print()

    print("=== Schedule base (all campaigns; max_new_leads_per_day per ICP) ===")
    print(json.dumps(SCHEDULE_BASE, indent=2))
    print("\n=== Settings (all campaigns) ===")
    print(json.dumps(SETTINGS, indent=2))

    if not args.push:
        print("\nDry-run only. Pass --push to apply.")
        return

    print("\n=== Pushing to Smartlead ===")
    for icp, group in plan.items():
        cid = campaign_ids.get(icp)
        if not cid:
            print(f"  [skip] {icp}: no campaign id"); continue
        ids = [int(m["id"]) for m in group["mailboxes"]]
        print(f"  [{icp}] cid={cid}")
        try:
            push_emails(api_key, cid, ids)
            print(f"    + email_accounts: {len(ids)}")
        except Exception as e:
            print(f"    !! email_accounts failed: {e}")
        try:
            push_schedule(api_key, cid, group["max_new_leads_per_day"])
            print(f"    + schedule (max_new={group['max_new_leads_per_day']})")
        except Exception as e:
            print(f"    !! schedule failed: {e}")
        try:
            push_settings(api_key, cid)
            print(f"    + settings")
        except Exception as e:
            print(f"    !! settings failed: {e}")

    # Persist allocation map for reference.
    alloc_log = {
        icp: {
            "max_new_leads_per_day": group["max_new_leads_per_day"],
            "mailboxes": [{"id": int(m["id"]), "email": m["from_email"]} for m in group["mailboxes"]],
        }
        for icp, group in plan.items()
    }
    (OUT_DIR / "_mailbox_allocation.json").write_text(
        json.dumps(alloc_log, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_DIR / '_mailbox_allocation.json'}")
    print("Done. Open each campaign and click Start when ready.")


if __name__ == "__main__":
    main()
