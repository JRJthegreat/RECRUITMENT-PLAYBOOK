"""
Generate STATIC icebreakers for recruitment outreach (niche-agnostic).

The icebreaker text is fixed for every lead. Only the first name is injected.
This mirrors the healthcare playbook, but the columns are configurable via CLI
flags so it runs against any sheet schema.

Reads:  --col_dm_name (full DM name), --col_status (optional filter)
Writes: --col_first (first_name), --col_last (last_name), --col_icebreaker

Only processes rows where the status cell == --status_value (if a status column
is given). Resume-safe: skips rows where the icebreaker cell is already populated.

Run:
  python3 -W ignore generate_icebreaker.py --sheet_url "URL" --tab "TAB" \
    --col_dm_name N --col_status R --status_value found \
    --col_first S --col_last T --col_icebreaker U [--preview N]
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

WRITE_BATCH = 10

# Static icebreaker. {first_name} is the only variable; everything else is fixed.
STATIC_ICEBREAKER = (
    "Hi {first_name},\n\n"
    "Love how you keep the human side front and center when sourcing candidates, "
    "not just letting AI do all the work. It seems you care more about fit than fill."
)



# Casualization (canonical rules: casualize-names skill) — conservative,
# near-universal nicknames only; unknown names pass through unchanged.
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


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


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


def ensure_headers(service, sheet_id, tab_name, out_cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = max(idx for idx, _ in out_cols) + 1
    if current_cols < needed_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
    for col_idx, header in out_cols:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()


def flush(service, updates, sheet_id, tab_name, c_first, c_last, c_ice):
    if not updates:
        return
    data = []
    for u in updates:
        data.append({"range": f"'{tab_name}'!{col_letter(c_first)}{u['row']}", "values": [[u["first_name"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(c_last)}{u['row']}",  "values": [[u["last_name"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(c_ice)}{u['row']}",   "values": [[u["icebreaker"]]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_dm_name", default="N", help="Column with DM full name")
    parser.add_argument("--col_status", default="", help="Optional status column to filter on (blank = no filter)")
    parser.add_argument("--status_value", default="found", help="Row processed only if status cell == this")
    parser.add_argument("--col_first", default="S", help="Output column for first name")
    parser.add_argument("--col_last", default="T", help="Output column for last name")
    parser.add_argument("--col_icebreaker", default="U", help="Output column for icebreaker")
    parser.add_argument("--limit", type=int, default=0, help="Cap rows processed (test batches)")
    parser.add_argument("--preview", type=int, default=0, help="Show N examples without writing")
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    c_dm    = col_to_idx(args.col_dm_name)
    c_stat  = col_to_idx(args.col_status) if args.col_status else None
    c_first = col_to_idx(args.col_first)
    c_last  = col_to_idx(args.col_last)
    c_ice   = col_to_idx(args.col_icebreaker)
    status_value = args.status_value.strip().lower()

    service = get_service()
    if not args.preview:
        ensure_headers(service, sheet_id, tab_name, [
            (c_first, "first_name"), (c_last, "last_name"), (c_ice, "icebreaker"),
        ])

    last_col = col_letter(max(c_dm, c_stat or 0, c_first, c_last, c_ice))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        if c_stat is not None:
            status = row[c_stat].strip().lower() if len(row) > c_stat else ""
            if status != status_value:
                continue
        dm_name  = row[c_dm].strip() if len(row) > c_dm else ""
        existing = row[c_ice].strip() if len(row) > c_ice else ""
        if not dm_name or existing:
            continue
        parts      = dm_name.split()
        first_name = casualize_first(parts[0])
        last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
        pending.append({
            "row":        i + 2,
            "first_name": first_name,
            "last_name":  last_name,
            "icebreaker": STATIC_ICEBREAKER.format(first_name=first_name),
        })

    if args.limit:
        pending = pending[:args.limit]

    print("=== Generate Icebreakers (static) ===\n")
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
            flush(service, updates, sheet_id, tab_name, c_first, c_last, c_ice)
            updates = []
    if updates:
        flush(service, updates, sheet_id, tab_name, c_first, c_last, c_ice)

    print(f"\nDone - {len(pending)} icebreakers written.")


if __name__ == "__main__":
    main()
