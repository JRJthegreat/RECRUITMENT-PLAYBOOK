"""
Generate icebreaker copy for the 400 contacts with valid emails.

Reads:  col L (dm_first_name), col N (dm_email), col Q (clean_company)
Writes: col R (icebreaker)

Only processes rows where col N has a valid email (not blank, not "not_found").
Resume-safe: skips rows where col R already populated.
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

COL_DM_FIRST   = 11  # L
COL_DM_LAST    = 12  # M
COL_EMAIL      = 13  # N
COL_CLEAN_CO   = 16  # Q
COL_ICEBREAKER = 17  # R

BATCH = 50

TEMPLATE = (
    "hey {first_name},\n\n"
    "love that {company} still keeps the human side front and center when sourcing "
    "candidates, not just letting AI do all the work. Seems like you care more about "
    "the fit, not just the fill."
)


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


def main():
    ap = argparse.ArgumentParser(description="Generate icebreakers for valid contacts → col R")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    print("=== Generate Icebreakers ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col R exists
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 18:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 18 - col_count,
            }}]}
        ).execute()

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!R1",
        valueInputOption="RAW",
        body={"values": [["icebreaker"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:R"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    pending = []
    for i, row in enumerate(data_rows):
        email = cell(row, COL_EMAIL)
        if not email or email.lower() == "not_found":
            continue
        if cell(row, COL_ICEBREAKER):
            continue  # already done
        first_name = cell(row, COL_DM_FIRST)
        company = cell(row, COL_CLEAN_CO)
        if not first_name or not company:
            continue
        pending.append({
            "row_num": i + 2,
            "first_name": first_name,
            "company": company,
            "email": email,
        })

    print(f"Rows to process: {len(pending)}\n")

    if args.dry_run:
        for p in pending[:5]:
            body = TEMPLATE.format(first_name=p["first_name"], company=p["company"])
            print(f"  Row {p['row_num']} ({p['email']}):")
            print(f"  {body}")
            print()
        print("[DRY RUN] No writes.")
        return

    written = 0
    for b in range(0, len(pending), BATCH):
        chunk = pending[b:b + BATCH]
        updates = []
        for p in chunk:
            body = TEMPLATE.format(first_name=p["first_name"], company=p["company"])
            updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_ICEBREAKER)}{p['row_num']}",
                "values": [[body]],
            })
        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
                written += len(chunk)
                print(f"  Wrote {written}/{len(pending)}")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Write failed: {e}")
        time.sleep(0.3)

    print(f"\n=== Done — {written} icebreakers written to col R ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
