"""
Create Instantly campaign "Single Opening - Healthcare - LinkedIn" and push all
leads from the Single Opening tab that have an email + email body.

Lead fields:
  first_name, last_name, email, company_name, website,
  company_linkedin, lt1 (DM LinkedIn), personalization (email body), role

Sequence (4 steps — matched to best-performing HR campaigns):
  Step 1 (delay 2d) — main email:    {{personalization}} / Sent from my iPhone
  Step 2 (delay 3d) — bump:          Hi {{firstName}}, bumping this — is the {{role}} role still open?
  Step 3 (delay 3d) — check:         Hi {{firstName}}, quick one — still hiring for the {{role}} role or has it been parked? Happy to send over the details if you're still looking.
  Step 4 (delay 4d) — close:         Hi {{firstName}}, going to close this out. If the role opens back up, feel free to reach out.

Resume-safe: skips rows where col AH == "TRUE". Writes "TRUE" every 10 leads.

Usage:
  python3 -W ignore push_single_campaign.py --sheet_url "URL" [--dry_run] [--campaign_id "ID"]
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
CAMPAIGN_NAME     = "Single Opening - Healthcare - LinkedIn"
BATCH_SIZE        = 10

# Column indices (0-based)
COL_JOB_TITLE    = 0   # A
COL_JOB_LINK     = 3   # D — LinkedIn job posting URL
COL_COMPANY      = 12  # M
COL_ABOUT_LINK   = 19  # T — company LinkedIn page
COL_WEBSITE      = 20  # U
COL_DM_NAME      = 26  # AA
COL_DM_LINKEDIN  = 28  # AC — DM personal LinkedIn
COL_EMAIL        = 29  # AD
COL_CLEAN_CO     = 31  # AF
COL_EMAIL_BODY   = 32  # AG
COL_ADDED        = 33  # AH

TAB = "Single Opening"


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
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
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
    name = re.sub(
        r"\b(Dr\.?|MD|PhD|DO|NP|PA|RN|MBA|MPH|SHRM-CP|M\.Ed|LPC|CPA|VFR|Jr\.?|Sr\.?|II|III|IV)\b",
        "", full_name, flags=re.IGNORECASE,
    )
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r",", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split()
    if not parts:
        return full_name.split()[0] if full_name.split() else "there", ""
    return parts[0], parts[-1] if len(parts) > 1 else ""


def clean_role(job_title):
    """Normalize clinical role title to a short conversational label."""
    t = (job_title or "").strip()
    tl = t.lower()
    if "physician assistant" in tl or "pa-c" in tl:
        return "PA"
    if any(x in tl for x in ("nurse practitioner", "fnp", "aprn", "np-c", "family np")):
        return "Nurse Practitioner"
    if "physician" in tl and "assistant" not in tl:
        return "Physician"
    # strip parentheticals and clean up
    t = re.sub(r'\s*\(.*?\)', '', t).strip()
    return t or "NP"


def strip_about(url):
    u = (url or "").strip().rstrip("/")
    if u.lower().endswith("/about"):
        u = u[:-len("/about")]
    return u


def ensure_columns(service, sheet_id, title, min_cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"appendDimension": {
                        "sheetId": gid, "dimension": "COLUMNS",
                        "length": min_cols - have}}]},
                ).execute()
            return


def create_campaign(name):
    """Create campaign. Sending accounts NOT set — Jude configures those manually."""
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
                    # Step 1 — main email
                    "type": "email",
                    "delay": 2,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "​{{firstName}}, quick one",
                        "body": "<div>{{personalization}}</div><div><br /></div><div>Sent from my iPhone</div>",
                    }],
                },
                {
                    # Step 2 — day 3 bump
                    "type": "email",
                    "delay": 3,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>Hi {{firstName}}, bumping this — is the {{role}} role still open?</div>"
                            "<div><br /></div><div>Cheers</div>"
                            "<div>Sent from my iPhone</div>"
                        ),
                    }],
                },
                {
                    # Step 3 — day 6 check-in
                    "type": "email",
                    "delay": 3,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>Hi {{firstName}}, quick one — still hiring for the {{role}} role "
                            "or has it been parked? Happy to send over the details if you're still looking.</div>"
                            "<div><br /></div><div>Cheers</div>"
                            "<div>Sent from my iPhone</div>"
                        ),
                    }],
                },
                {
                    # Step 4 — day 10 soft close
                    "type": "email",
                    "delay": 4,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>Hi {{firstName}}, going to close this out. "
                            "If the role opens back up, feel free to reach out.</div>"
                            "<div><br /></div><div>Cheers</div>"
                            "<div>Sent from my iPhone</div>"
                        ),
                    }],
                },
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
    resp = requests.post(
        f"{INSTANTLY_BASE}/campaigns",
        headers=instantly_headers(),
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json().get("id") or resp.json().get("data", {}).get("id")


def push_lead(campaign_id, payload):
    data = dict(payload)
    data["campaign"] = campaign_id
    try:
        resp = requests.post(
            f"{INSTANTLY_BASE}/leads",
            headers=instantly_headers(),
            json=data,
            timeout=30,
        )
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.RequestException as e:
        return False, 0, str(e)


def main():
    ap = argparse.ArgumentParser(description="Create Instantly campaign + push Single Opening leads")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--campaign_id", default="", help="Use existing campaign (skip creation)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set")
        return

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    print(f"=== Push Single Opening → Instantly {'[DRY RUN]' if args.dry_run else ''} ===\n")

    ensure_columns(service, sheet_id, TAB, COL_ADDED + 1)
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
            {"range": f"'{TAB}'!{col_letter(COL_ADDED)}1", "values": [["Added to Instantly"]]}
        ]}).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB}'!A2:AH10000"
    ).execute().get("values", [])

    seen_emails = set()
    leads = []
    for i, r in enumerate(rows):
        email = cell(r, COL_EMAIL)
        body = cell(r, COL_EMAIL_BODY)
        added = cell(r, COL_ADDED)
        if not email or not body or added.upper() == "TRUE" or email in seen_emails:
            continue
        seen_emails.add(email)
        first_name, last_name = split_name(cell(r, COL_DM_NAME))
        role = clean_role(cell(r, COL_JOB_TITLE))
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":            email,
                "first_name":       first_name,
                "last_name":        last_name,
                "company_name":     cell(r, COL_CLEAN_CO) or cell(r, COL_COMPANY),
                "website":          cell(r, COL_WEBSITE),
                "personalization":  body,
                "custom_variables": {
                    "Role":              role,
                    "Job Link":          cell(r, COL_JOB_LINK),
                    "LinkedIn_Url":      cell(r, COL_DM_LINKEDIN),
                    "Company_Linkedin":  strip_about(cell(r, COL_ABOUT_LINK)),
                },
            },
        })

    print(f"Leads to push: {len(leads)}")

    if args.dry_run:
        for lead in leads[:3]:
            p = lead["payload"]
            cv = p["custom_variables"]
            print(f"\n  Row {lead['row_num']}: {p['email']}")
            print(f"    name:             {p['first_name']} {p['last_name']}")
            print(f"    company:          {p['company_name']}")
            print(f"    Role:             {cv['Role']}")
            print(f"    Job Link:         {cv['Job Link']}")
            print(f"    LinkedIn_Url:     {cv['LinkedIn_Url']}")
            print(f"    Company_Linkedin: {cv['Company_Linkedin']}")
            print(f"    personalization:  {p['personalization'][:100].replace(chr(10),' ↵ ')}...")
        print(f"\n[DRY RUN] No API calls.")
        return

    # Create or reuse campaign
    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"Using existing campaign: {campaign_id}")
    else:
        print(f"Creating campaign: {CAMPAIGN_NAME!r}...")
        campaign_id = create_campaign(CAMPAIGN_NAME)
        print(f"  Created: {campaign_id}")

    print(f"\nPushing {len(leads)} leads...\n")

    added = failed = 0
    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
        else:
            print(f"  [!] Row {lead['row_num']} failed ({status}): {text}")
            failed += 1

        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            updates = [
                {"range": f"'{TAB}'!{col_letter(COL_ADDED)}{leads[j]['row_num']}",
                 "values": [["TRUE"]]}
                for j in range(batch_start, i + 1)
            ]
            for attempt in range(3):
                try:
                    service.spreadsheets().values().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={"valueInputOption": "RAW", "data": updates},
                    ).execute()
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(5)
                    else:
                        print(f"  [!] Sheet write failed: {e}")
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {failed} failed)")
            time.sleep(1.0)

    print(f"\n=== Done ===")
    print(f"  Campaign: {CAMPAIGN_NAME}")
    print(f"  ID:       {campaign_id}")
    print(f"  Pushed:   {added}")
    print(f"  Failed:   {failed}")


if __name__ == "__main__":
    main()
