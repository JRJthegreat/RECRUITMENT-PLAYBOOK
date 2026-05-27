"""
Fetch a Sales Navigator Apify dataset, dedupe by LinkedIn company ID against
the existing sheet (col F), and append net-new companies as new rows.
Run enrich_agencies.py afterward to enrich the appended rows.

Usage:
  python3 pull_agencies_dataset.py --sheet_url "..." --dataset_id "fPNocPBjYu5olPw6U"
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

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"

BATCH = 10

# Column indices matching the existing Sales Navigator sheet layout
COL_COMPANY   = 0   # A
COL_DESC      = 1   # B
COL_EMP_RANGE = 2   # C
COL_EMP_COUNT = 3   # D
COL_URN       = 4   # E
COL_ID        = 5   # F
COL_INDUSTRY  = 6   # G
COL_LIST      = 7   # H
COL_LOGO      = 8   # I
COL_SAVED     = 9   # J
# K–X (10–23): spotlightBadges — left blank for new rows
COL_TRACKING  = 24  # Y
# Z (25): linkedin_url  — written by enrich_agencies.py
# AA (26): website      — written by enrich_agencies.py
TOTAL_COLS    = 27


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


def fetch_dataset(dataset_id):
    """Fetch all items from an Apify dataset, paginated."""
    items = []
    limit = 500
    offset = 0
    while True:
        r = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": APIFY_TOKEN, "limit": limit, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return items


def item_to_row(item):
    """Map a Sales Navigator dataset item to a sheet row (list of 27 values)."""
    row = [""] * TOTAL_COLS
    row[COL_COMPANY]   = str(item.get("companyName") or "")
    row[COL_DESC]      = str(item.get("description") or "")
    row[COL_EMP_RANGE] = str(item.get("employeeCountRange") or "")
    row[COL_EMP_COUNT] = str(item.get("employeeDisplayCount") or "")
    row[COL_URN]       = str(item.get("entityUrn") or "")
    row[COL_ID]        = str(item.get("id") or "")
    row[COL_INDUSTRY]  = str(item.get("industry") or "")
    row[COL_LIST]      = str(item.get("listCount") or "")
    row[COL_LOGO]      = str(item.get("logo") or "")
    row[COL_SAVED]     = str(item.get("saved") or "")
    row[COL_TRACKING]  = str(item.get("trackingId") or "")
    return row


def main():
    ap = argparse.ArgumentParser(description="Pull Apify dataset → dedupe → append to sheet")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dataset_id", required=True, help="Apify dataset ID")
    ap.add_argument("--dry_run", action="store_true", help="Preview without writing")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    print(f"=== Pull Agencies Dataset → {args.dataset_id} ===\n")

    # Fetch dataset
    print("Fetching Apify dataset...")
    items = fetch_dataset(args.dataset_id)
    print(f"  {len(items)} items fetched")

    # Load existing sheet IDs
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"  Tab: '{tab_name}'")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!F:F"
    ).execute()
    existing_ids = {
        str(r[0]).strip()
        for r in result.get("values", [])[1:]  # skip header
        if r and r[0]
    }
    print(f"  Existing companies in sheet: {len(existing_ids)}")

    # Dedupe
    new_items = [i for i in items if str(i.get("id") or "").strip() not in existing_ids]
    print(f"  Net-new companies to append: {len(new_items)}")

    if not new_items:
        print("\nNothing to append — all companies already in sheet.")
        return

    if args.dry_run:
        print("\nSample of new companies (first 10):")
        for item in new_items[:10]:
            print(f"  {item.get('id'):<12} {item.get('companyName')}")
        return

    # Get current row count so we know where to start writing
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    col_count = 0
    row_count = 0
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            row_count = s["properties"]["gridProperties"]["rowCount"]
            break

    # Ensure enough columns
    if col_count < TOTAL_COLS:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": TOTAL_COLS - col_count,
            }}]}
        ).execute()

    # Ensure enough rows
    needed_rows = row_count + len(new_items)
    if needed_rows > row_count:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "ROWS",
                "length": len(new_items),
            }}]}
        ).execute()

    # Find the first truly empty row (first row where col A is blank)
    col_a = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A"
    ).execute()
    next_row = len(col_a.get("values", [])) + 1  # 1-based, after last row with data

    print(f"  Writing from row {next_row}")

    # Write in batches of 10 using explicit row ranges
    appended = 0
    num_batches = (len(new_items) + BATCH - 1) // BATCH
    for b in range(num_batches):
        chunk = new_items[b * BATCH:(b + 1) * BATCH]
        rows = [item_to_row(item) for item in chunk]
        end_row = next_row + len(chunk) - 1
        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": [{
                        "range": f"'{tab_name}'!A{next_row}:AA{end_row}",
                        "values": rows,
                    }]},
                ).execute()
                appended += len(chunk)
                next_row += len(chunk)
                print(f"  Batch {b + 1}/{num_batches}: wrote rows {next_row - len(chunk)}–{next_row - 1} ({appended} total)")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Write failed: {e}")
        if b < num_batches - 1:
            time.sleep(0.5)

    print(f"\n=== Done — {appended} new rows appended ===")
    print(f"Now run enrich_agencies.py to enrich the new rows.")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
