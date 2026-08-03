"""
Generate icebreakers for Texas healthcare staffing agency outreach.

Template:
  hey {first_name},

  love how you still keep the human side front and center when sourcing candidates,
  not just letting AI do all the work. Seems like you care more about the fit, not just the fill.

Reads:  col A (Company Name), col N (dm_name), col R (email_status)
Writes: col S (first_name), col T (last_name), col U (icebreaker)

Only processes rows where email_status == "found".
Resume-safe: skips rows where col U already populated.

Run:
  python3 -W ignore generate_icebreaker.py --sheet_url "URL" --tab "TAB" [--preview N]
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

COL_DM_NAME      = 13  # N
COL_EMAIL_STATUS = 17  # R
COL_FIRST_NAME   = 18  # S
COL_LAST_NAME    = 19  # T
COL_ICEBREAKER   = 20  # U

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


def ensure_headers(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = COL_ICEBREAKER + 1
    if current_cols < needed_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
    for col_idx, header in [
        (COL_FIRST_NAME, "first_name"),
        (COL_LAST_NAME,  "last_name"),
        (COL_ICEBREAKER, "icebreaker"),
    ]:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()



# Casualization (canonical rules: casualize-names skill) — conservative list.
NICKNAMES = {
    "William": "Will", "Michael": "Mike", "Christopher": "Chris",
    "Matthew": "Matt", "Daniel": "Dan", "Benjamin": "Ben",
    "Nicholas": "Nick", "Alexander": "Alex", "Jonathan": "Jon",
    "Timothy": "Tim", "Jeffrey": "Jeff", "Gregory": "Greg",
    "Joshua": "Josh", "Robert": "Rob", "Richard": "Rich",
    "Thomas": "Tom", "Kenneth": "Ken", "Joseph": "Joe",
    "Edward": "Ed", "Donald": "Don", "Ronald": "Ron",
    "Steven": "Steve", "Stephen": "Steve", "David": "Dave",
    "Douglas": "Doug", "Lawrence": "Larry", "Frederick": "Fred",
    "Raymond": "Ray", "Jennifer": "Jen", "Elizabeth": "Liz",
    "Katherine": "Kate", "Kathleen": "Kathy", "Stephanie": "Steph",
    "Samantha": "Sam", "Jacqueline": "Jackie", "Deborah": "Deb",
    "Pamela": "Pam", "Cynthia": "Cindy", "Rebecca": "Becca",
}


def casualize_first(name):
    return NICKNAMES.get((name or "").strip().title(), (name or "").strip())


def build_icebreaker(first_name):
    first_name = casualize_first(first_name)
    return (
        f"hey {first_name},\n\n"
        f"love how you still keep the human side front and center when sourcing candidates, "
        f"not just letting AI do all the work. Seems like you care more about the fit, not just the fill."
    )


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = []
    for u in updates:
        data.append({"range": f"'{tab_name}'!{col_letter(COL_FIRST_NAME)}{u['row']}", "values": [[u["first_name"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_LAST_NAME)}{u['row']}",  "values": [[u["last_name"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_ICEBREAKER)}{u['row']}",  "values": [[u["icebreaker"]]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--preview", type=int, default=0, help="Show N examples without writing")
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    service = get_service()
    ensure_headers(service, sheet_id, tab_name)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:U"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        status     = row[COL_EMAIL_STATUS].strip().lower() if len(row) > COL_EMAIL_STATUS else ""
        dm_name    = row[COL_DM_NAME].strip() if len(row) > COL_DM_NAME else ""
        existing   = row[COL_ICEBREAKER].strip() if len(row) > COL_ICEBREAKER else ""
        if status != "found" or not dm_name or existing:
            continue
        parts      = dm_name.split()
        first_name = parts[0]
        last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
        pending.append({
            "row":        i + 2,
            "first_name": first_name,
            "last_name":  last_name,
            "icebreaker": build_icebreaker(first_name),
        })

    print(f"=== Generate Icebreakers ===\n")
    print(f"Rows to process: {len(pending)}\n")

    if args.preview:
        for p in pending[:args.preview]:
            print(f"Row {p['row']}  {p['first_name']} {p['last_name']}")
            print(p["icebreaker"])
            print()
        return

    updates = []
    for p in pending:
        updates.append(p)
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab_name)
            updates = []
    if updates:
        flush(service, updates, sheet_id, tab_name)

    print(f"\nDone — {len(pending)} icebreakers written.")


if __name__ == "__main__":
    main()
