"""
Split a healthcare recruitment-firm sheet into two tabs by column B (Headcount).

  Tab "1-50 EMP"    -> headcount 1..50  (50 inclusive)
  Tab "50-200 EMP"  -> headcount 51..200

Source tab is left untouched. Target tabs are created if missing and
fully overwritten on each run (idempotent). Rows are written in batches.

Run:
  python3 -W ignore split_by_headcount.py --sheet_url "URL" --source_tab "TAB"
"""

import json
import argparse
import re
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

HEADCOUNT_COL = 1  # B
TAB_LOW = "1-50 EMP"
TAB_HIGH = "50-200 EMP"
WRITE_BATCH = 2000


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


def parse_sheet_id(sheet_url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {sheet_url}")
    return m.group(1)


def num(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None


def ensure_tab(service, sheet_id, title, ncols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    if title in existing:
        sid = existing[title]["sheetId"]
        # clear contents
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{title}'"
        ).execute()
        return sid
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {
            "title": title,
            "gridProperties": {"rowCount": 11000, "columnCount": max(ncols, 26)},
        }}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_rows(service, sheet_id, title, header, rows):
    # header
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{title}'!A1",
        valueInputOption="RAW", body={"values": [header]},
    ).execute()
    r = 2
    for i in range(0, len(rows), WRITE_BATCH):
        batch = rows[i:i + WRITE_BATCH]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{title}'!A{r}",
            valueInputOption="RAW", body={"values": batch},
        ).execute()
        r += len(batch)
        print(f"  {title}: wrote rows up to {r-1}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--source_tab", required=True)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    service = get_service()

    vals = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{args.source_tab}'!A1:Z100000"
    ).execute().get("values", [])
    header = vals[0]
    data = vals[1:]
    print(f"Source rows: {len(data)}", flush=True)

    low, high, dropped = [], [], 0
    for row in data:
        hc = num(row[HEADCOUNT_COL]) if len(row) > HEADCOUNT_COL else None
        if hc is None:
            dropped += 1
            continue
        if 1 <= hc <= 50:
            low.append(row)
        elif 51 <= hc <= 200:
            high.append(row)
        else:
            dropped += 1

    print(f"1-50: {len(low)} | 50-200: {len(high)} | dropped(out-of-range/blank): {dropped}", flush=True)

    ncols = len(header)
    ensure_tab(service, sheet_id, TAB_LOW, ncols)
    ensure_tab(service, sheet_id, TAB_HIGH, ncols)
    write_rows(service, sheet_id, TAB_LOW, header, low)
    write_rows(service, sheet_id, TAB_HIGH, header, high)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
