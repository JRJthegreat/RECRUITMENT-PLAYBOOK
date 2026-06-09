"""
Create "Healthcare Staffing - Competitor Angle" Instantly campaign and push
215 leads from the campaign_v2 tab.

Reads:  col A  (companyName raw), col J (company_linkedin), col K (website),
        col L (dm_first_name), col M (dm_last_name), col N (dm_email),
        col O (dm_title), col P (dm_linkedin), col Q (clean_company),
        col W (email_body_v2 — full email incl. icebreaker)
Writes: col V (added_to_instantly) → "TRUE" after each batch

Resume-safe: skips rows where col V already == "TRUE".
NOTE: col W already contains the full email — personalization = col W directly,
      do NOT prepend icebreaker.
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
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"
CAMPAIGN_NAME     = "Healthcare Staffing - Competitor Angle"

BATCH_SIZE = 10

COL_COMPANY_RAW  = 0   # A
COL_CO_LI        = 9   # J
COL_WEBSITE      = 10  # K
COL_DM_FIRST     = 11  # L
COL_DM_LAST      = 12  # M
COL_EMAIL        = 13  # N
COL_DM_TITLE     = 14  # O
COL_DM_LI        = 15  # P
COL_CLEAN_CO     = 16  # Q
COL_BODY_V2      = 22  # W — full email (icebreaker already included)
COL_ADDED        = 21  # V


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def create_campaign():
    payload = {
        "name": CAMPAIGN_NAME,
        "campaign_schedule": {
            "schedules": [{
                "name": "New schedule",
                "timing": {"from": "07:00", "to": "18:00"},
                "days": {"1": True, "2": True, "3": True, "4": True, "5": True, "6": True},
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
                        "subject": "routing some reqs your way",
                        "body": "<div>{{personalization}}<br /><br /><br />Sent from my iPhone<br /><br /></div>",
                    }],
                },
                {
                    "type": "email",
                    "delay": 2,
                    "delay_unit": "days",
                    "variants": [{
                        "subject": "",
                        "body": (
                            "<div>hey {{firstName}},</div><div><br /></div>"
                            "<div>Just bumping this up in case it got buried.<br />"
                            "These medical practices have roles going unfilled for weeks. "
                            "Patients going unattended and revenue walking out the door.<br /><br />"
                            "Let me know if you are open to some warm intros.<br /><br />"
                            "Best,<br />Jude</div><div><br /></div><div>Sent from my iPhone</div>"
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
                            "<div>Last note from me.</div><div><br /></div>"
                            "<div>No worries if connecting with these medical practices is not a priority right now.</div>"
                            "<div><br /></div>"
                            "<div>When timing is right, feel free to reopen. I am one reply away.<br /><br />"
                            "Best,<br />Jude</div><div><br /></div><div>Sent from my iPhone</div>"
                        ),
                    }],
                },
            ]
        }],
        "daily_limit": 2500,
        "stop_on_reply": True,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": False,
    }
    resp = requests.post(
        f"{INSTANTLY_BASE}/campaigns",
        headers=instantly_headers(),
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Campaign creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return data.get("id") or data.get("campaign_id") or data["id"]


def push_lead(campaign_id, payload):
    body = dict(payload)
    body["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(),
                             json=body, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


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


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
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
    ap = argparse.ArgumentParser(description="Create campaign + push campaign_v2 leads to Instantly")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--campaign_id", default="",
                    help="Skip campaign creation and use this existing campaign ID")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set in .env")
        return

    print(f"=== Push Campaign v2 → Instantly ===\n")
    service = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(service, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Resolve campaign
    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"Campaign ID (provided): {campaign_id}")
    elif args.dry_run:
        campaign_id = "DRY_RUN"
        print(f"Campaign: [dry run — no creation]")
    else:
        print(f"Creating campaign '{CAMPAIGN_NAME}'...")
        campaign_id = create_campaign()
        print(f"Campaign ID: {campaign_id}\n")

    # Ensure col V exists
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 22:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 22 - col_count,
            }}]}
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!V1",
        valueInputOption="RAW",
        body={"values": [["added_to_instantly"]]},
    ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:W"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        email = cell(row, COL_EMAIL)
        if not email:
            continue
        if cell(row, COL_ADDED).upper() == "TRUE":
            continue
        body_v2 = cell(row, COL_BODY_V2)
        if not body_v2:
            continue
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":            email,
                "first_name":       cell(row, COL_DM_FIRST),
                "last_name":        cell(row, COL_DM_LAST),
                "company_name":     cell(row, COL_CLEAN_CO),
                "personalization":  body_v2,
                "website":          cell(row, COL_WEBSITE),
                "company_linkedin": cell(row, COL_CO_LI),
                "dm_linkedin":      cell(row, COL_DM_LI),
                "title":            cell(row, COL_DM_TITLE),
                "company_name_raw": cell(row, COL_COMPANY_RAW),
            },
        })

    print(f"Leads to push: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['email']}")
            print(f"    first_name:       {p['first_name']}")
            print(f"    last_name:        {p['last_name']}")
            print(f"    company_name:     {p['company_name']}")
            print(f"    company_name_raw: {p['company_name_raw']}")
            print(f"    title:            {p['title']}")
            print(f"    website:          {p['website']}")
            print(f"    company_linkedin: {p['company_linkedin']}")
            print(f"    dm_linkedin:      {p['dm_linkedin']}")
            print(f"    personalization preview:")
            preview = p["personalization"][:300].replace("\n", " ↵ ")
            print(f"      {preview}")
            print()
        print("[DRY RUN] No API calls made.")
        return

    added, failed = add_leads(campaign_id, leads, service, sheet_id, tab_name)

    print(f"\n=== Done ===")
    print(f"  Campaign: {CAMPAIGN_NAME}")
    print(f"  ID:       {campaign_id}")
    print(f"  Pushed:   {added}")
    print(f"  Failed:   {failed}")
    print(f"  Sheet:    https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
