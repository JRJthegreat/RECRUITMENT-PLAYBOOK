"""
Push healthcare agency leads to an existing Instantly campaign.

Reads:  col J (linkedin_url/company_linkedin), col K (website),
        col L (dm_first_name), col M (dm_last_name), col N (dm_email),
        col P (dm_linkedin), col Q (clean_company),
        col R (icebreaker), col S (email_body)
Writes: col V (added_to_instantly) → "TRUE" after each batch

Personalization = icebreaker + blank line + email body (combined).
Resume-safe: skips rows where col V already == "TRUE".
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

BATCH_SIZE = 10

COL_CO_LI     = 9   # J — company LinkedIn
COL_WEBSITE   = 10  # K
COL_DM_FIRST  = 11  # L
COL_DM_LAST   = 12  # M
COL_EMAIL     = 13  # N
COL_DM_LI     = 15  # P — DM LinkedIn
COL_CLEAN_CO  = 16  # Q
COL_ICEBREAKER = 17  # R
COL_BODY      = 18  # S
COL_ADDED     = 21  # V


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def push_lead(campaign_id, lead):
    payload = dict(lead)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(),
                             json=payload, timeout=30)
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

    # Retry failed leads
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
    ap = argparse.ArgumentParser(description="Push agency leads to Instantly campaign")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--campaign_id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set in .env")
        return

    print("=== Push Agency Leads → Instantly ===\n")
    service = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(service, sheet_id, args.sheet_url)
    print(f"Tab:      '{tab_name}'")
    print(f"Campaign: {args.campaign_id}\n")

    # Ensure col V exists + write header
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
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:V"
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
        icebreaker = cell(row, COL_ICEBREAKER)
        body = cell(row, COL_BODY)
        if not icebreaker or not body:
            continue
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":             email,
                "first_name":        cell(row, COL_DM_FIRST),
                "last_name":         cell(row, COL_DM_LAST),
                "company_name":      cell(row, COL_CLEAN_CO),
                "personalization":   f"{icebreaker}\n\n{body}",
                "website":           cell(row, COL_WEBSITE),
                "company_linkedin":  cell(row, COL_CO_LI),
                "dm_linkedin":       cell(row, COL_DM_LI),
            },
        })

    print(f"Leads to push: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['email']}")
            print(f"    first_name:   {p['first_name']}")
            print(f"    company_name: {p['company_name']}")
            print(f"    personalization preview:")
            preview = p['personalization'][:200].replace('\n', ' ↵ ')
            print(f"      {preview}...")
            print()
        print(f"[DRY RUN] No API calls.")
        return

    added, failed = add_leads(args.campaign_id, leads, service, sheet_id, tab_name)

    print(f"\n=== Done ===")
    print(f"  Pushed:  {added}")
    print(f"  Failed:  {failed}")
    print(f"  Sheet:   https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
