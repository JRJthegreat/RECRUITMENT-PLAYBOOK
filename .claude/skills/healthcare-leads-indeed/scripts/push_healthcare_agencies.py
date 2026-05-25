"""
Push healthcare staffing agency leads to Instantly.
Creates campaign "Healthcare Recruitment - May 25th" with 4-step sequence,
pushes leads with firstName, lastName, companyName, website, companyLinkedIn,
dmLinkedIn, and personalization. Marks pushed rows in col Y.
"""

import os
import re
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE = "https://api.instantly.ai/api/v2"

CAMPAIGN_NAME = "Healthcare Recruitment - May 25th"
BATCH_SIZE = 10

# Column indices (0-based)
COL_EMAIL          = 16  # Q
COL_WEBSITE        = 11  # L
COL_CO_LINKEDIN    = 5   # F
COL_DM_LINKEDIN    = 17  # R
COL_FIRST_NAME     = 20  # U
COL_LAST_NAME      = 21  # V
COL_CLEAN_COMPANY  = 22  # W
COL_BODY           = 23  # X
COL_ADDED          = 24  # Y


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def list_sending_accounts():
    resp = requests.get(f"{INSTANTLY_BASE}/accounts", headers=instantly_headers(),
                        params={"limit": 100}, timeout=30)
    resp.raise_for_status()
    return [a["email"] for a in resp.json().get("items", [])]


def create_campaign():
    sending_accounts = list_sending_accounts()
    if not sending_accounts:
        raise RuntimeError("No sending accounts found in Instantly")
    print(f"  Sending accounts: {sending_accounts}")

    payload = {
        "name": CAMPAIGN_NAME,
        "email_list": sending_accounts,
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
                    "type": "email",
                    "delay": 2,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "​{{firstName}}, quick one",
                        "body": "<div>{{personalization}}<br /><br />Sent from my iPhone</div>",
                    }],
                },
                {
                    "type": "email",
                    "delay": 3,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>hey {{firstName}},</div><div><br /></div>"
                            "<div>Is connecting with these private practices something worth exploring for you right now?</div>"
                            "<div><br /></div><div>Happy to make the intro.</div>"
                            "<div><br /></div><div>Sent from my iPhone</div>"
                        ),
                    }],
                },
                {
                    "type": "email",
                    "delay": 4,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>hey {{firstName}},</div><div><br /></div>"
                            "<div>Just bumping this up in case it got buried.</div>"
                            "<div><br /></div>"
                            "<div>Still have a few practices open to introductions — let me know if the timing works.</div>"
                            "<div><br /></div><div>Sent from my iPhone</div>"
                        ),
                    }],
                },
                {
                    "type": "email",
                    "delay": 5,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>hey {{firstName}},</div><div><br /></div>"
                            "<div>last note from me.</div><div><br /></div>"
                            "<div>no worries if connecting with these medical practices is not a priority right now.</div>"
                            "<div><br /></div>"
                            "<div>when timing is right, feel free to reopen. I am one reply away</div>"
                            "<div><br /></div><div>Sent from my iPhone</div>"
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

    resp = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=instantly_headers(),
                         json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json().get("id")


def push_lead(campaign_id, lead):
    payload = dict(lead)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(),
                             json=payload, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


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
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_gid_from_url(url):
    m = re.search(r"gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"], s["properties"]["sheetId"]
    s = meta["sheets"][0]
    return s["properties"]["title"], s["properties"]["sheetId"]


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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
            failed_set = {idx for idx, _ in failed}
            updates = [
                {"range": f"'{tab_name}'!{col_letter(COL_ADDED)}{lead['row_num']}",
                 "values": [["TRUE"]]}
                for j, lead in enumerate(leads[((i // BATCH_SIZE) * BATCH_SIZE):i + 1], (i // BATCH_SIZE) * BATCH_SIZE)
                if j not in failed_set
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

    # Retry failed
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
    ap = argparse.ArgumentParser(description="Push healthcare agency leads to Instantly")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--campaign_id", default="", help="Existing campaign ID (skip creation)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set in .env")
        return

    print("=== Push Healthcare Agency Leads → Instantly ===\n")
    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(service, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col Y exists + write header
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 25:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 25 - col_count,
            }}]}
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!Y1",
        valueInputOption="RAW",
        body={"values": [["Added to Instantly"]]},
    ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:Y"
    ).execute()
    data_rows = result.get("values", [])[1:]

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        if cell(row, COL_ADDED).upper() == "TRUE":
            continue
        email = cell(row, COL_EMAIL)
        if not email or email == "not_found":
            continue
        body = cell(row, COL_BODY)
        if not body:
            continue
        first_name = cell(row, COL_FIRST_NAME)
        personalization = f"hey {first_name}\n\n{body}" if first_name else body

        leads.append({
            "row_num": i + 2,
            "payload": {
                "email": email,
                "first_name": first_name,
                "last_name": cell(row, COL_LAST_NAME),
                "company_name": cell(row, COL_CLEAN_COMPANY),
                "website": cell(row, COL_WEBSITE),
                "personalization": personalization,
                "custom_variables": {
                    "companyLinkedIn": cell(row, COL_CO_LINKEDIN),
                    "dmLinkedIn": cell(row, COL_DM_LINKEDIN),
                },
            },
        })

    print(f"  {len(leads)} leads to push\n")

    if args.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['email']} | {p['first_name']} {p['last_name']} | {p['company_name']}")
            print(f"    personalization: {p['custom_variables']['personalization'][:100]}...")
            print()
        return

    # Create or use existing campaign
    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"  Using existing campaign: {campaign_id}")
    else:
        print(f"  Creating campaign '{CAMPAIGN_NAME}'...")
        campaign_id = create_campaign()
        print(f"  Campaign created: {campaign_id}\n")

    added, still_failed = add_leads(campaign_id, leads, service, sheet_id, tab_name)

    print(f"\n=== Done — {added} pushed, {still_failed} failed ===")
    print(f"Campaign ID: {campaign_id}")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
