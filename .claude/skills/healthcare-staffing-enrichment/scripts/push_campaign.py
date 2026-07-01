"""
Push Texas healthcare staffing agency leads to a new Instantly campaign.

Creates the campaign (DRAFT), then pushes all verified leads one at a time.
Activate the campaign manually in Instantly UI when ready.

Reads:  col B (company_domain), col P (dm_email), col Q (dm_linkedin),
        col R (email_status), col S (first_name), col T (last_name),
        col V (email_body / personalization)
Writes: col W (added_to_instantly) → "TRUE" after each batch of 10

Resume-safe: skips rows where col W already == "TRUE".

Run:
  python3 -W ignore push_campaign.py \
    --sheet_url "URL" --tab "TAB" --campaign_name "NAME" [--limit N] [--dry_run]
"""

import os
import re
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"

BATCH_SIZE = 10

COL_DOMAIN       = 1   # B
COL_DM_EMAIL     = 15  # P
COL_DM_LINKEDIN  = 16  # Q
COL_EMAIL_STATUS = 17  # R
COL_FIRST_NAME   = 18  # S
COL_LAST_NAME    = 19  # T
COL_EMAIL_BODY   = 21  # V
COL_ADDED        = 22  # W


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def get_service():
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


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def create_campaign(name):
    payload = {
        "name": name,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "New schedule",
                    "timing": {"from": "09:00", "to": "18:00"},
                    "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
                    "timezone": "America/Detroit",
                }
            ]
        },
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": 2,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "​{{firstName}}, quick one",
                                "body": "<div>{{personalization}} <br /><br />Sent from my iPhone</div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}}, <br /><br />just bumping this - still open to taking on new reqs?<br /> <br />Happy to make the intro.<br /><br />Best,<br />Jude<br /><br />Sent from my iPhone</div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}}, real quick — still looking for fresh reqs or all good on pipeline?</div><div><br /></div><div>Happy to connect you if you're still searching.</div><div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}}, </div><div>I'll assume timing's not right. If you're ever looking for fresh healthcare reqs down the line, feel free to reach out.</div><div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>",
                            }
                        ],
                    },
                ]
            }
        ],
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
    resp = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=instantly_headers(),
                         json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()["id"]


def push_lead(campaign_id, lead):
    payload = dict(lead)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(),
                             json=payload, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def ensure_col_w(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    if current_cols < COL_ADDED + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": (COL_ADDED + 1) - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(COL_ADDED)}1",
        valueInputOption="RAW",
        body={"values": [["added_to_instantly"]]},
    ).execute()


def add_leads(campaign_id, leads, service, sheet_id, tab_name):
    added = 0
    failed = []

    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
        else:
            print(f"  Lead {i+1} failed ({status}): {text}")
            failed.append((i, lead))

        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            failed_indices = {idx for idx, _ in failed}
            updates = [
                {"range": f"'{tab_name}'!{col_letter(COL_ADDED)}{lead['row_num']}",
                 "values": [["TRUE"]]}
                for j, lead in enumerate(leads[batch_start:i + 1], batch_start)
                if j not in failed_indices
            ]
            if updates:
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
                            print(f"  Sheet write failed: {e}")
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {len(failed)} failed)")
            if i + 1 < len(leads):
                time.sleep(1.5)

    if failed:
        print(f"\n  Retrying {len(failed)} failed leads...")
        for attempt in range(1, 4):
            still_failing = []
            time.sleep(5 * attempt)
            for orig_idx, lead in failed:
                ok, status, text = push_lead(campaign_id, lead["payload"])
                if ok:
                    added += 1
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id,
                            body={"valueInputOption": "RAW", "data": [{
                                "range": f"'{tab_name}'!{col_letter(COL_ADDED)}{lead['row_num']}",
                                "values": [["TRUE"]],
                            }]},
                        ).execute()
                    except Exception:
                        pass
                else:
                    still_failing.append((orig_idx, lead))
            print(f"  Retry {attempt}: {len(failed) - len(still_failing)} recovered, {len(still_failing)} still failing")
            failed = still_failing
            if not failed:
                break

    return added, len(failed)


def main():
    parser = argparse.ArgumentParser(description="Create Instantly campaign and push TX healthcare agency leads")
    parser.add_argument("--sheet_url",     required=True)
    parser.add_argument("--tab",           required=True)
    parser.add_argument("--campaign_name", required=True, help="Name for the new Instantly campaign")
    parser.add_argument("--limit",  type=int, default=0)
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set in .env"); return

    print("=== Push TX Healthcare Agency Leads → Instantly ===\n")
    service   = get_service()
    sheet_id  = parse_sheet_id(args.sheet_url)
    tab_name  = args.tab

    ensure_col_w(service, sheet_id, tab_name)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:W"
    ).execute().get("values", [])[1:]
    print(f"Total rows: {len(rows)}")

    leads = []
    for i, row in enumerate(rows):
        if args.limit and len(leads) >= args.limit:
            break
        if cell(row, COL_EMAIL_STATUS).lower() != "found":
            continue
        email = cell(row, COL_DM_EMAIL)
        if not email:
            continue
        body = cell(row, COL_EMAIL_BODY)
        if not body:
            continue
        if cell(row, COL_ADDED).upper() == "TRUE":
            continue
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":           email,
                "first_name":      cell(row, COL_FIRST_NAME),
                "last_name":       cell(row, COL_LAST_NAME),
                "linkedin":        cell(row, COL_DM_LINKEDIN),
                "company_domain":  cell(row, COL_DOMAIN),
                "personalization": body,
            },
        })

    print(f"Leads to push: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['email']}")
            print(f"    first_name:     {p['first_name']}")
            print(f"    last_name:      {p['last_name']}")
            print(f"    company_domain: {p['company_domain']}")
            print(f"    linkedin:       {p['linkedin']}")
            print(f"    personalization preview:")
            print(f"      {p['personalization'][:200].replace(chr(10), ' ↵ ')}...")
            print()
        print("[DRY RUN] No API calls made.")
        return

    print(f"Creating campaign '{args.campaign_name}'...")
    campaign_id = create_campaign(args.campaign_name)
    print(f"  Campaign created: {campaign_id}\n")

    print(f"Pushing {len(leads)} leads (batches of {BATCH_SIZE})...")
    added, failed = add_leads(campaign_id, leads, service, sheet_id, tab_name)

    print(f"\n=== Done ===")
    print(f"  Pushed:      {added}")
    print(f"  Failed:      {failed}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"  Sheet:       https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print(f"\n  Campaign created as DRAFT — activate in Instantly when ready.")


if __name__ == "__main__":
    main()
