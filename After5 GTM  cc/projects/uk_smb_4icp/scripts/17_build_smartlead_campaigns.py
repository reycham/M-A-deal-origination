"""Build 4 Smartlead campaigns (one per ICP), draft/paused state, ready for review.

Phases:
  A. Parse the email-template markdown (4 ICPs * 3 emails) into a dict.
  B. Build per-ICP lead lists from final_v6/_all_deliverable_ranked.csv.
  C. Create campaign + add leads + save 3-step sequence via Smartlead REST API.
     Sequence delays: E1 -> 3d -> E2 -> 4d -> E3. Campaigns left paused (no
     mailbox, no schedule). User attaches mailbox + schedule + clicks Start
     in the UI.
  D. Audit log + URLs.

Default mode is dry-run (no API calls). Pass --push to actually create campaigns.

Flags:
  --push                 actually call the Smartlead API
  --icp "<name>"         restrict to one ICP (Real estate / Mortgage / Dealership / Recruitment)
  --limit N              cap leads per ICP (smoke testing)
  --dry-print            extra-verbose body preview in dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.config import get_key  # noqa: E402

MASTER_CSV = PROJECT_ROOT / "output" / "final_v6" / "_all_deliverable_ranked.csv"
TEMPLATES_MD = REPO_ROOT / "After5 Digital — SmartLead Email Templates with Spintax.md"
OUT_DIR = PROJECT_ROOT / "output" / "smartlead"

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"

# Markdown ICP heading -> our canonical ICP label (must match master CSV `icp` column).
ICP_HEADINGS = {
    "REAL ESTATE":      "Real estate",
    "RECRUITMENT":      "Recruitment",
    "MORTGAGE BROKERS": "Mortgage",
    "CAR DEALERSHIPS":  "Dealership",
}
ICP_ORDER = ["Real estate", "Mortgage", "Dealership", "Recruitment"]

# Sequence delays in days. delay[0] is for step 1 (always 0).
SEQ_DELAYS = [0, 3, 4]

# Excluded contacts (platform domains noted in CLAUDE.md).
EXCLUDE_COMPANIES = {"RIDA", "Salon Privé", "Tender365"}


# --------------------------------------------------------------------------- #
# Phase A: parse templates                                                    #
# --------------------------------------------------------------------------- #

def _strip_word_escapes(s: str) -> str:
    """MS Word inserts backslash escapes in front of underscore/hash inside markdown."""
    return s.replace("\\_", "_").replace("\\#", "#").replace("\\-", "-")


def _strip_md_bold(s: str) -> str:
    return re.sub(r"\*\*", "", s)


def parse_templates(md_path: Path) -> dict:
    """Returns {icp_label: {"E1": {"subject": str, "body": str}, ...}}.

    Strategy: walk the markdown line-by-line. Track:
      - current ICP (set when we see a `**HEADING**` matching ICP_HEADINGS)
      - current email number (set when we see `**Email N**`)
      - current section: "subject" or "body" (set when we see `**Subject:**`/`**Body:**`)
    Accumulate non-empty lines into the right bucket, end body when we hit the
    next `**Email N**` or new ICP heading.
    """
    raw = md_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    out: dict[str, dict[str, dict[str, str]]] = {v: {} for v in ICP_HEADINGS.values()}
    cur_icp: str | None = None
    cur_email: str | None = None
    cur_section: str | None = None  # "subject" | "body"
    buf: list[str] = []

    def flush():
        nonlocal buf
        if cur_icp and cur_email and cur_section and buf:
            text = "\n".join(buf).strip()
            if cur_email not in out[cur_icp]:
                out[cur_icp][cur_email] = {"subject": "", "body": ""}
            out[cur_icp][cur_email][cur_section] = text
        buf = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # ICP heading: a single bold line that matches one of our headings.
        m_icp = re.fullmatch(r"\*\*([A-Z ]+)\*\*", stripped)
        if m_icp and m_icp.group(1).strip() in ICP_HEADINGS:
            flush()
            cur_icp = ICP_HEADINGS[m_icp.group(1).strip()]
            cur_email = None
            cur_section = None
            continue

        m_email = re.fullmatch(r"\*\*Email\s+([123])\*\*", stripped)
        if m_email:
            flush()
            cur_email = f"E{m_email.group(1)}"
            cur_section = None
            continue

        if re.fullmatch(r"\*\*Subject:\*\*", stripped):
            flush()
            cur_section = "subject"
            continue
        if re.fullmatch(r"\*\*Body:\*\*", stripped):
            flush()
            cur_section = "body"
            continue

        # Skip blank lines bordering the section.
        if not stripped:
            if buf:
                buf.append("")  # preserve paragraph break inside body
            continue

        # Strip word escapes + remove bold markers from regular content.
        cleaned = _strip_md_bold(_strip_word_escapes(stripped))
        if cur_section:
            buf.append(cleaned)

    flush()

    # Reconciliation: rename {{personalised_opener}} -> {{personalized_opener}}
    # and unify subject for E3 (the markdown writes "(no subject — same thread)").
    for icp, emails in out.items():
        for ek, blk in emails.items():
            blk["subject"] = blk["subject"].replace("{{personalised_opener}}", "{{personalized_opener}}")
            blk["body"] = blk["body"].replace("{{personalised_opener}}", "{{personalized_opener}}")
            # Strip parenthetical "(no subject ...)" notation -> empty subject.
            if blk["subject"].lower().startswith("(no subject"):
                blk["subject"] = ""

    # Validate completeness.
    for icp in ICP_ORDER:
        for ek in ("E1", "E2", "E3"):
            if ek not in out[icp] or not out[icp][ek].get("body"):
                raise RuntimeError(f"Template parse failure: {icp} / {ek} missing body")
    return out


def body_text_to_html(body: str) -> str:
    """Smartlead expects HTML in email_body. Convert plain-text paragraphs (separated
    by blank lines) to <p>...</p> blocks. Preserve spintax {a|b|c} verbatim.
    """
    # Split on 2+ newlines for paragraphs; single newlines inside become <br>.
    paras = re.split(r"\n\s*\n", body.strip())
    html_paras = []
    for p in paras:
        p_html = p.strip().replace("\n", "<br>\n")
        if p_html:
            html_paras.append(f"<p>{p_html}</p>")
    return "\n".join(html_paras)


# --------------------------------------------------------------------------- #
# Phase B: build per-ICP leads                                                #
# --------------------------------------------------------------------------- #

def build_leads_per_icp(master_csv: Path) -> dict[str, list[dict]]:
    df = pd.read_csv(master_csv, dtype=str, keep_default_na=False)
    # Filters (most are already enforced upstream, but be defensive).
    df = df[df["tier"].isin(["Hot", "Warm", "Cool", "Cold"])]
    df = df[df["final_email"] != ""]
    df = df[df["personalized_opener"] != ""]
    df = df[df["company_short"] != ""]
    df = df[~df["Company Name"].isin(EXCLUDE_COMPANIES)]

    # Pick first/last name from the right persona block.
    def first_name(row):
        return row["sales_first_name"] if row["persona"] == "sales" else row["founder_first_name"]
    def last_name(row):
        return row["sales_last_name"] if row["persona"] == "sales" else row["founder_last_name"]

    df["_first_name"] = df.apply(first_name, axis=1)
    df["_last_name"] = df.apply(last_name, axis=1)
    df = df[df["_first_name"] != ""]

    leads_by_icp: dict[str, list[dict]] = {icp: [] for icp in ICP_ORDER}
    for _, r in df.iterrows():
        icp = r["icp"]
        if icp not in leads_by_icp:
            continue
        # Smartlead does NOT recursively expand placeholders inside custom-field
        # values, so substitute {{company_short}} into the opener at upload time.
        opener_resolved = r["personalized_opener"].replace(
            "{{company_short}}", r["company_short"])
        leads_by_icp[icp].append({
            "email": r["final_email"],
            "first_name": r["_first_name"],
            "last_name": r["_last_name"],
            "company_name": r["Company Name"],
            "custom_fields": {
                "company_short": r["company_short"],
                "personalized_opener": opener_resolved,
                "tier": r["tier"],
                "lead_score": r["lead_score"],
                "linkedin_url": r["LinkedIn"],
                "persona": r["persona"],
            },
        })
    return leads_by_icp


# --------------------------------------------------------------------------- #
# Phase C: Smartlead REST API                                                 #
# --------------------------------------------------------------------------- #

class Smartlead:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.s = requests.Session()

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{SMARTLEAD_BASE}{path}{sep}api_key={self.api_key}"

    def create_campaign(self, name: str) -> int:
        r = self.s.post(self._url("/campaigns/create"),
                        json={"name": name}, timeout=30)
        r.raise_for_status()
        data = r.json()
        cid = data.get("id") or data.get("campaign_id") or data.get("data", {}).get("id")
        if not cid:
            raise RuntimeError(f"create_campaign: no id in response: {data}")
        return int(cid)

    def add_leads(self, campaign_id: int, leads: list[dict]) -> dict:
        body = {
            "lead_list": leads,
            "settings": {
                "ignore_global_block_list": False,
                "ignore_unsubscribe_list": False,
                "ignore_community_bounce_list": False,
                "ignore_duplicate_leads_in_other_campaign": False,
            },
        }
        r = self.s.post(self._url(f"/campaigns/{campaign_id}/leads"),
                        json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def save_sequence(self, campaign_id: int, steps: list[dict]) -> dict:
        body = {"sequences": steps}
        r = self.s.post(self._url(f"/campaigns/{campaign_id}/sequences"),
                        json=body, timeout=30)
        r.raise_for_status()
        return r.json()


def build_sequence_steps(icp_templates: dict[str, dict[str, str]]) -> list[dict]:
    steps = []
    for i, ek in enumerate(("E1", "E2", "E3")):
        blk = icp_templates[ek]
        steps.append({
            "seq_number": i + 1,
            "seq_delay_details": {"delay_in_days": SEQ_DELAYS[i]},
            "subject": blk["subject"],
            "email_body": body_text_to_html(blk["body"]),
        })
    return steps


# --------------------------------------------------------------------------- #
# Main orchestration                                                          #
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--icp", type=str, default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-print", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Phase A
    print("=== Phase A: parse templates ===")
    templates = parse_templates(TEMPLATES_MD)
    (OUT_DIR / "_parsed_templates.json").write_text(
        json.dumps(templates, indent=2, ensure_ascii=False), encoding="utf-8")
    for icp in ICP_ORDER:
        for ek in ("E1", "E2", "E3"):
            blk = templates[icp][ek]
            n_words = len(blk["body"].split())
            print(f"  [{icp:12s}] {ek}: subject_len={len(blk['subject'])}, body_words={n_words}")

    # Phase B
    print("\n=== Phase B: build leads per ICP ===")
    leads_by_icp = build_leads_per_icp(MASTER_CSV)
    total = 0
    for icp in ICP_ORDER:
        leads = leads_by_icp[icp]
        if args.limit:
            leads = leads[: args.limit]
            leads_by_icp[icp] = leads
        # Audit CSV.
        rows = [{
            "email": l["email"],
            "first_name": l["first_name"],
            "last_name": l["last_name"],
            "company_name": l["company_name"],
            **{f"cf_{k}": v for k, v in l["custom_fields"].items()},
        } for l in leads]
        pd.DataFrame(rows).to_csv(OUT_DIR / f"{icp}_leads.csv", index=False)
        print(f"  [{icp:12s}] {len(leads)} leads -> {OUT_DIR / (icp + '_leads.csv')}")
        total += len(leads)
    print(f"  TOTAL leads: {total}")

    if args.dry_print:
        print("\n--- Sample E1 body for Real estate ---")
        print(templates["Real estate"]["E1"]["body"])
        print("\n--- Sample lead (Real estate) ---")
        if leads_by_icp["Real estate"]:
            print(json.dumps(leads_by_icp["Real estate"][0], indent=2, ensure_ascii=False))

    if not args.push:
        print("\nDry-run complete (no API calls). Pass --push to create campaigns.")
        return

    # Phase C
    print("\n=== Phase C: push to Smartlead (paused) ===")
    api_key = get_key("SMARTLEAD_API_KEY")
    sl = Smartlead(api_key)

    icp_filter = [args.icp] if args.icp else ICP_ORDER
    icp_ids: dict[str, int] = {}
    push_log_rows = []
    for icp in icp_filter:
        if icp not in leads_by_icp:
            print(f"  [skip] unknown ICP: {icp}")
            continue
        leads = leads_by_icp[icp]
        if not leads:
            print(f"  [skip] {icp}: 0 leads")
            continue

        ts = datetime.now(timezone.utc).strftime("%Y-%m")
        campaign_name = f"After5 — UK SMB — {icp} — {ts}"
        print(f"  Creating campaign: {campaign_name}")
        try:
            cid = sl.create_campaign(campaign_name)
        except Exception as e:
            print(f"    !! create_campaign failed: {e}")
            push_log_rows.append({"icp": icp, "campaign_id": "", "leads_pushed": 0,
                                  "step": "create_campaign", "error": str(e)})
            continue
        icp_ids[icp] = cid
        print(f"    campaign_id = {cid}")

        # Add leads in batches of 100.
        pushed = 0
        for i in range(0, len(leads), 100):
            batch = leads[i:i + 100]
            try:
                sl.add_leads(cid, batch)
                pushed += len(batch)
                print(f"    pushed {pushed}/{len(leads)}")
            except Exception as e:
                print(f"    !! add_leads batch {i//100} failed: {e}")
                push_log_rows.append({"icp": icp, "campaign_id": cid, "leads_pushed": pushed,
                                      "step": f"add_leads_batch_{i//100}", "error": str(e)})
                break
            time.sleep(0.4)  # be gentle

        # Save sequence.
        try:
            steps = build_sequence_steps(templates[icp])
            sl.save_sequence(cid, steps)
            print(f"    sequence saved (3 steps, delays={SEQ_DELAYS})")
        except Exception as e:
            print(f"    !! save_sequence failed: {e}")
            push_log_rows.append({"icp": icp, "campaign_id": cid, "leads_pushed": pushed,
                                  "step": "save_sequence", "error": str(e)})

        push_log_rows.append({"icp": icp, "campaign_id": cid, "leads_pushed": pushed,
                              "step": "done", "error": ""})
        print(f"    https://app.smartlead.ai/app/email-campaigns/{cid}")

    # Persist artefacts.
    (OUT_DIR / "_campaign_ids.json").write_text(
        json.dumps(icp_ids, indent=2), encoding="utf-8")
    pd.DataFrame(push_log_rows).to_csv(OUT_DIR / "_push_log.csv", index=False)
    print(f"\nDone. Campaign IDs in {OUT_DIR / '_campaign_ids.json'}")
    print("Campaigns are PAUSED. Open Smartlead UI, attach mailbox(es) + schedule, then click Start.")


if __name__ == "__main__":
    main()
