"""
Create a new 'campaign_v2' tab in the agencies sheet containing leads ready for
the competitor-framed campaign (col W filled, col V != TRUE).

Reads:  cols A-W from the agencies tab
Writes: new tab 'campaign_v2' with those rows + headers
        Row height pinned to 18px

Usage:
    python3 -W ignore create_campaign_v2_tab.py --sheet_url "URL" [--tab_name campaign_v2]
"""

import os
import re
import json
import time
import argparse
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

COL_EMAIL  = 13  # N
COL_BODY_V2 = 22  # W
COL_ADDED  = 21  # V


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


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def pad_row(row, width):
    """Ensure row has exactly `width` cells."""
    return list(row) + [""] * (width - len(row)) if len(row) < width else list(row[:width])


def main():
    ap = argparse.ArgumentParser(description="Copy campaign-v2-ready leads to a new tab")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab_name", default="campaign_v2",
                    help="Name for the new tab (default: campaign_v2)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Delete and recreate the tab if it already exists")
    args = ap.parse_args()

    print("=== Create campaign_v2 Tab ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    source_tab, source_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Source tab: '{source_tab}'")

    # Read all data A:W
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{source_tab}'!A:W"
    ).execute()
    all_rows = result.get("values", [])
    if not all_rows:
        print("No data found.")
        return

    headers = all_rows[0]
    data_rows = all_rows[1:]
    N_COLS = 23  # A-W

    print(f"Total data rows: {len(data_rows)}")

    # Filter: email present, NOT added to current campaign, email_body_v2 filled
    ready = []
    for row in data_rows:
        email = cell(row, COL_EMAIL)
        if not email or email.lower() == "not_found":
            continue
        if cell(row, COL_ADDED).upper() == "TRUE":
            continue
        if not cell(row, COL_BODY_V2):
            continue
        ready.append(pad_row(row, N_COLS))

    print(f"Ready for campaign_v2: {len(ready)}\n")

    if not ready:
        print("Nothing to write — run generate_agency_emails_v2.py first.")
        return

    # Check if tab already exists
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing_tabs = {s["properties"]["title"]: s["properties"]["sheetId"]
                     for s in meta["sheets"]}

    if args.tab_name in existing_tabs:
        if args.overwrite:
            print(f"Deleting existing tab '{args.tab_name}'...")
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"deleteSheet": {
                    "sheetId": existing_tabs[args.tab_name]
                }}]}
            ).execute()
            time.sleep(1)
        else:
            print(f"Tab '{args.tab_name}' already exists. Use --overwrite to replace it.")
            return

    # Create new tab
    print(f"Creating tab '{args.tab_name}'...")
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {
            "properties": {"title": args.tab_name}
        }}]}
    ).execute()
    new_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    time.sleep(0.5)

    # Write headers + data in one call
    padded_headers = pad_row(headers, N_COLS)
    all_write = [padded_headers] + ready

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{args.tab_name}'!A1",
        valueInputOption="RAW",
        body={"values": all_write},
    ).execute()
    print(f"  Wrote {len(ready)} rows + header")

    # Pin row height to 18px
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "updateDimensionProperties": {
                "range": {
                    "sheetId": new_gid,
                    "dimension": "ROWS",
                    "startIndex": 0,
                    "endIndex": len(ready) + 1,
                },
                "properties": {"pixelSize": 18},
                "fields": "pixelSize",
            }
        }]}
    ).execute()
    print(f"  Row heights set to 18px")

    print(f"\n=== Done ===")
    print(f"  Tab '{args.tab_name}': {len(ready)} leads ready for campaign v2")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
