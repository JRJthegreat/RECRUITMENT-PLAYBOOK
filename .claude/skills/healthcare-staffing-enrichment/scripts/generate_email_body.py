"""
Generate full email bodies for Texas healthcare staffing agency outreach.

Combines icebreaker (col U) with fixed body template → col V (email_body).

Resume-safe: skips rows where col V already populated.

Run:
  python3 -W ignore generate_email_body.py --sheet_url "URL" --tab "TAB"
"""

import os
import re
import json
import time
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

COL_EMAIL_STATUS = 17  # R
COL_ICEBREAKER   = 20  # U
COL_EMAIL_BODY   = 21  # V

BODY_TEMPLATE = (
    "Noticed your work helping healthcare employers in TX fill clinical roles. Impressive stuff.\n\n"
    "I'm in active conversations with a few small medical practices struggling to fill critical vacancies "
    "with no internal TA team. They're actively open to working with external recruiters. "
    "Rather than running the searches myself, I've been routing these to specialized recruiters.\n\n"
    "Could intro you if you're looking for fresh reqs.\n\n"
    "Worth a quick 15 min chat?\n\n"
    "Best,\n"
    "Jude"
)

WRITE_BATCH = 10


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def ensure_col(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = COL_EMAIL_BODY + 1
    if current_cols < needed_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}1",
        valueInputOption="RAW",
        body={"values": [["email_body"]]},
    ).execute()


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = [
        {"range": f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}{u['row']}", "values": [[u["body"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    service = get_service()
    ensure_col(service, sheet_id, tab_name)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:V"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        status     = row[COL_EMAIL_STATUS].strip().lower() if len(row) > COL_EMAIL_STATUS else ""
        icebreaker = row[COL_ICEBREAKER].strip() if len(row) > COL_ICEBREAKER else ""
        existing   = row[COL_EMAIL_BODY].strip() if len(row) > COL_EMAIL_BODY else ""
        if status != "found" or not icebreaker or existing:
            continue
        pending.append({
            "row":  i + 2,
            "body": f"{icebreaker}\n\n{BODY_TEMPLATE}",
        })

    print(f"=== Generate Email Bodies ===\n")
    print(f"Rows to process: {len(pending)}\n")

    updates = []
    for p in pending:
        updates.append(p)
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab_name)
            updates = []
    if updates:
        flush(service, updates, sheet_id, tab_name)

    print(f"\nDone — {len(pending)} email bodies written to col V.")


if __name__ == "__main__":
    main()
