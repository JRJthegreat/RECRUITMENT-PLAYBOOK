"""
Generate the personalized email body (col V) for the healthcare recruitment-firm
demand campaign, from the approved fixed template with {first_name} substituted.

Output is HTML (campaign is not text-only) used as the {{personalization}} variable.

Reads:  col S (email_status), col T (first_name)
Writes: col V (email_body)

Only processes rows where email_status == "found" and first_name is set.
Resume-safe: skips rows where col V already populated.

Run:
  python3 -W ignore generate_demand_body.py --sheet_url "URL" --tab "TAB" [--preview N]
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

COL_EMAIL_STATUS = 18  # S
COL_FIRST_NAME   = 19  # T
COL_EMAIL_BODY   = 21  # V

WRITE_BATCH = 10

# Approved body. {first} is substituted per lead. Plain-text lines; HTML-ized below.
BODY_LINES = [
    "Hey {first},",
    "",
    "Love how you still keep the human side front and center when sourcing candidates, "
    "not just letting AI do all the work. Seems like you care more about the fit, not just the fill.",
    "",
    "I'm in touch with healthcare employers across the US that are expanding to new locations, "
    "and most want a pipeline in place before the hiring crunch hits. Rather than run the searches "
    "myself, I route those reqs to specialist firms like yours.",
    "",
    "Are you open to new reqs right now, or are you already at capacity?",
    "",
    "Best,",
    "Jude",
]


def build_body(first):
    html = []
    for line in BODY_LINES:
        if line == "":
            html.append("<div><br /></div>")
        else:
            html.append(f"<div>{line.format(first=first)}</div>")
    return "".join(html)


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
    if current_cols < COL_EMAIL_BODY + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": (COL_EMAIL_BODY + 1) - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}1",
        valueInputOption="RAW", body={"values": [["email_body"]]},
    ).execute()


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}{u['row']}", "values": [[u["body"]]]}
            for u in updates]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--preview", type=int, default=0)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    ensure_col(service, sheet_id, tab)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:V"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        status = row[COL_EMAIL_STATUS].strip().lower() if len(row) > COL_EMAIL_STATUS else ""
        first  = row[COL_FIRST_NAME].strip() if len(row) > COL_FIRST_NAME else ""
        body   = row[COL_EMAIL_BODY].strip() if len(row) > COL_EMAIL_BODY else ""
        if status != "found" or not first or body:
            continue
        pending.append({"row": i + 2, "body": build_body(first)})

    print(f"=== Generate Email Bodies — tab '{tab}' ===")
    print(f"Rows to process: {len(pending)}\n", flush=True)

    if args.preview:
        for p in pending[:args.preview]:
            print(f"Row {p['row']}:")
            print(p["body"].replace("</div>", "</div>\n"))
            print()
        print("[PREVIEW] No writes.")
        return

    updates = []
    for p in pending:
        updates.append(p)
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab); updates = []
    if updates:
        flush(service, updates, sheet_id, tab)
    print(f"\nDone — {len(pending)} bodies written to col V.")


if __name__ == "__main__":
    main()
