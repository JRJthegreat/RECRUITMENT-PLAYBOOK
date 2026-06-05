"""
Create an Instantly campaign and push leads from an opening tab (healthcare Indeed sheet)
with full variables + a personalized subject line.

Lead fields:
  email, first_name, last_name, company_name (clean), website, personalization (email body)
  custom_variables: subject_line, Role, DM_Title, Job Link, Company_Website

Sequence (4 steps, approved framework):
  Step 1 — subject {{subject_line}} (personalized), body {{personalization}}
  Step 2 (day 3) — bump: is the {{Role}} role still open?
  Step 3 (day 6) — still hiring for the {{Role}} role or parked?
  Step 4 (day 10) — soft close

NO sending accounts (Jude configures those). Resume-safe via col AL "Pushed to Campaign".

Usage:
  python3 -W ignore push_opening_campaign.py --sheet_url "URL" --tab "Single Opening" \
      --campaign_name "Healthcare - June 2nd - Single Openings" [--dry_run] [--limit N] [--campaign_id ID]
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"
BATCH_SIZE        = 10

# Single Opening tab columns (0-based)
COL_COMPANY    = 10  # K
COL_WEBSITE    = 11  # L
COL_DM_NAME    = 19  # T
COL_DM_TITLE   = 20  # U
COL_EMAIL      = 22  # W
COL_ROLE       = 27  # AB  (specialty-specific, e.g. "oncology NP")
COL_JOB_LINK   = 28  # AC  (Indeed URL)
COL_CLEAN_CO   = 33  # AH
COL_EMAIL_BODY = 35  # AJ
COL_SUBJECT    = 36  # AK
COL_PUSHED     = 37  # AL  (resume flag — fresh column, avoids stale Z/AA)


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def get_google_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"], token_uri=td["token_uri"],
        client_id=td["client_id"], client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def split_name(full_name):
    if not full_name:
        return "there", ""
    name = re.sub(r"\b(Dr\.?|MD|PhD|DO|NP|PA|RN|MBA|MPH|Jr\.?|Sr\.?|II|III|IV|CHCR|MSN|LPC|LCSW|FNP|APRN|DNP|FACP)\b",
                  "", full_name, flags=re.IGNORECASE)
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r",", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split()
    if not parts:
        return full_name.split()[0] if full_name.split() else "there", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def ensure_columns(svc, sid, tab, min_cols):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"appendDimension": {
                    "sheetId": gid, "dimension": "COLUMNS", "length": min_cols - have}}]}).execute()
            return


# Follow-up step bodies per variant (Step 1 is always personalization; these are steps 2-4)
FOLLOWUPS = {
    "single": [
        ("<div>Hi {{firstName}}, bumping this, is the {{Role}} role still open?</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
        ("<div>Hi {{firstName}}, quick one, still hiring for the {{Role}} role "
         "or has it been parked? Happy to send over the details if you're still looking.</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
        ("<div>Hi {{firstName}}, going to close this out. "
         "If the role opens back up, feel free to reach out.</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
    ],
    "multiple": [
        ("<div>Hi {{firstName}}, bumping this up. Are those {{Role}} roles still open?</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
        ("<div>Hi {{firstName}}, since you've got a few roles open at once, happy to make a quick "
         "intro to the person I mentioned if any of them are urgent.</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
        ("<div>Hi {{firstName}}, last one from me on this. If filling those roles is still a "
         "priority, just say the word and I'll connect you.</div>"
         "<div><br /></div><div>Cheers</div><div>Sent from my iPhone</div>"),
    ],
}


def create_campaign(name, variant="single"):
    """Create campaign. NO sending accounts — Jude sets those in the UI."""
    fu = FOLLOWUPS.get(variant, FOLLOWUPS["single"])
    payload = {
        "name": name,
        "campaign_schedule": {
            "schedules": [{
                "name": "New schedule",
                "timing": {"from": "09:00", "to": "18:00"},
                "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
                "timezone": "America/Detroit",
            }]
        },
        "sequences": [{
            "steps": [
                {
                    "type": "email", "delay": 2, "delay_unit": "days",
                    "variants": [{
                        "subject": "{{subject_line}}",
                        "body": "<div>{{personalization}}</div><div><br /></div><div>Sent from my iPhone</div>",
                    }],
                },
                {"type": "email", "delay": 3, "delay_unit": "days",
                 "variants": [{"subject": "", "body": fu[0]}]},
                {"type": "email", "delay": 3, "delay_unit": "days",
                 "variants": [{"subject": "", "body": fu[1]}]},
                {"type": "email", "delay": 4, "delay_unit": "days",
                 "variants": [{"subject": "", "body": fu[2]}]},
            ]
        }],
        "daily_limit": 2500,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": True,
        "first_email_text_only": True,
        "prioritize_new_leads": False,
        "stop_for_company": False,
    }
    resp = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=instantly_headers(), json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json().get("id") or resp.json().get("data", {}).get("id")


def push_lead(campaign_id, payload):
    data = dict(payload)
    data["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(), json=data, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.RequestException as e:
        return False, 0, str(e)


def main():
    ap = argparse.ArgumentParser(description="Push opening-tab leads to a new Instantly campaign")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--campaign_name", required=True)
    ap.add_argument("--campaign_id", default="", help="Use existing campaign (skip creation)")
    ap.add_argument("--variant", choices=["single", "multiple"], default="single",
                    help="Follow-up wording set (default single)")
    ap.add_argument("--limit", type=int, default=0, help="Cap leads pushed (test runs)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set")
        return

    svc = get_google_service()
    sid = get_sheet_id_from_url(args.sheet_url)

    print(f"=== Push {args.tab} → Instantly {'[DRY RUN]' if args.dry_run else ''} ===\n")

    ensure_columns(svc, sid, args.tab, COL_PUSHED + 1)
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
        {"range": f"'{args.tab}'!{col_letter(COL_PUSHED)}1", "values": [["Pushed to Campaign"]]}
    ]}).execute()

    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A2:AL10000"
    ).execute().get("values", [])

    # Group rows by email so multi-row companies become ONE lead carrying all their row_nums.
    # Skip an email if ANY of its rows is already TRUE (resume-safe); mark ALL its rows on push.
    from collections import OrderedDict
    by_email = OrderedDict()
    for i, r in enumerate(rows):
        email = cell(r, COL_EMAIL); body = cell(r, COL_EMAIL_BODY)
        if not email or not body:
            continue
        e = by_email.setdefault(email, {"row": r, "row_nums": [], "pushed": False})
        e["row_nums"].append(i + 2)
        if cell(r, COL_PUSHED).upper() == "TRUE":
            e["pushed"] = True

    leads = []
    for email, info in by_email.items():
        if info["pushed"]:
            continue
        r = info["row"]
        first, last = split_name(cell(r, COL_DM_NAME))
        website = cell(r, COL_WEBSITE)
        body = cell(r, COL_EMAIL_BODY)
        leads.append({
            "row_nums": info["row_nums"],
            "payload": {
                "email":           email,
                "first_name":      first,
                "last_name":       last,
                "company_name":    cell(r, COL_CLEAN_CO) or cell(r, COL_COMPANY),
                "website":         website,
                "personalization": body,
                "custom_variables": {
                    "subject_line":    cell(r, COL_SUBJECT),
                    "Role":            cell(r, COL_ROLE) or "NP",
                    "DM_Title":        cell(r, COL_DM_TITLE),
                    "Job Link":        cell(r, COL_JOB_LINK),
                    "Company_Website": website,
                },
            },
        })
        if args.limit and len(leads) >= args.limit:
            break

    print(f"Leads to push: {len(leads)}")

    if args.dry_run:
        for lead in leads[:3]:
            p = lead["payload"]; cv = p["custom_variables"]
            print(f"\n  Rows {lead['row_nums']}: {p['email']}")
            print(f"    name:            {p['first_name']} {p['last_name']}")
            print(f"    company_name:    {p['company_name']}")
            print(f"    website:         {p['website']}")
            print(f"    subject_line:    {cv['subject_line']}")
            print(f"    Role:            {cv['Role']}")
            print(f"    DM_Title:        {cv['DM_Title']}")
            print(f"    Job Link:        {cv['Job Link']}")
            print(f"    personalization: {p['personalization'][:90].replace(chr(10),' / ')}...")
        print("\n[DRY RUN] No API calls.")
        return

    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"Using existing campaign: {campaign_id}")
    else:
        print(f"Creating campaign: {args.campaign_name!r} (no sending accounts, variant={args.variant})...")
        campaign_id = create_campaign(args.campaign_name, args.variant)
        print(f"  Created: {campaign_id}")

    print(f"\nPushing {len(leads)} leads...\n")
    added = failed = 0
    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
        else:
            print(f"  [!] Rows {lead['row_nums']} failed ({status}): {text}")
            failed += 1
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            start = (i // BATCH_SIZE) * BATCH_SIZE
            updates = [{"range": f"'{args.tab}'!{col_letter(COL_PUSHED)}{rn}", "values": [["TRUE"]]}
                       for j in range(start, i + 1) for rn in leads[j]["row_nums"]]
            for attempt in range(3):
                try:
                    svc.spreadsheets().values().batchUpdate(
                        spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(5)
                    else:
                        print(f"  [!] sheet write failed: {e}")
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {failed} failed)")
            time.sleep(1.0)

    print(f"\n=== Done ===\n  Campaign: {args.campaign_name}\n  ID: {campaign_id}\n  Pushed: {added}\n  Failed: {failed}")


if __name__ == "__main__":
    main()
