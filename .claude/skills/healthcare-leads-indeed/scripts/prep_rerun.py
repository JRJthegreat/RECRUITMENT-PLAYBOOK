"""
One-shot sheet cleanup before re-running clean_agency_names + generate_icebreakers.

For rows where col N (dm_email) has any value (email OR "not_found"):
  1. Write "true" to col T (verified)
  2. Clear "not_found" → blank in col N

For rows where col N has a valid email:
  3. Clear col Q (clean_company) so clean_agency_names.py re-processes them
  4. Clear col R (icebreaker) so generate_icebreakers.py re-processes them
"""

import os
import re
import json
import time
import argparse
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

COL_EMAIL     = 13  # N
COL_CLEAN_CO  = 16  # Q
COL_ICEBREAKER = 17  # R
COL_VERIFIED  = 19  # T


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


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def flush(service, sheet_id, updates, label=""):
    if not updates:
        return
    for attempt in range(3):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            if label:
                print(f"  Wrote {len(updates)} {label}")
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                print(f"  [!] Write failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    print("=== Prep Rerun — verified + clear not_found + reset Q/R ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col T exists
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 20:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 20 - col_count,
            }}]}
        ).execute()

    # Write header T1
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!T1",
        valueInputOption="RAW",
        body={"values": [["verified"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:T"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}\n")

    verified_updates = []
    clear_email_updates = []
    clear_q_updates = []
    clear_r_updates = []

    valid_count = 0
    not_found_count = 0

    for i, row in enumerate(data_rows):
        email = cell(row, COL_EMAIL)
        if not email:
            continue

        sheet_row = i + 2
        is_valid = email.lower() != "not_found"

        # Mark verified for all touched rows (email found or not_found)
        verified_updates.append({
            "range": f"'{tab_name}'!{col_letter(COL_VERIFIED)}{sheet_row}",
            "values": [["true"]],
        })

        if not is_valid:
            # Clear "not_found" → blank
            clear_email_updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_EMAIL)}{sheet_row}",
                "values": [[""]],
            })
            not_found_count += 1
        else:
            # Clear clean_company + icebreaker so scripts re-process
            clear_q_updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_CLEAN_CO)}{sheet_row}",
                "values": [[""]],
            })
            clear_r_updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_ICEBREAKER)}{sheet_row}",
                "values": [[""]],
            })
            valid_count += 1

    print(f"  Rows to mark verified:     {len(verified_updates)}")
    print(f"  Rows to clear not_found:   {not_found_count}")
    print(f"  Rows to reset Q+R (valid): {valid_count}\n")

    if args.dry_run:
        print("[DRY RUN] No writes.")
        return

    BATCH = 500
    for b in range(0, len(verified_updates), BATCH):
        flush(svc, sheet_id, verified_updates[b:b + BATCH], "verified")
        time.sleep(0.3)

    for b in range(0, len(clear_email_updates), BATCH):
        flush(svc, sheet_id, clear_email_updates[b:b + BATCH], "not_found cleared")
        time.sleep(0.3)

    all_clears = clear_q_updates + clear_r_updates
    for b in range(0, len(all_clears), BATCH):
        flush(svc, sheet_id, all_clears[b:b + BATCH], "Q+R cleared")
        time.sleep(0.3)

    print(f"\n=== Done ===")
    print(f"  verified=true written: {len(verified_updates)}")
    print(f"  not_found cleared:     {not_found_count}")
    print(f"  Q+R reset for rerun:   {valid_count}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
