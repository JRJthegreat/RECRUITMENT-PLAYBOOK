"""
One-shot: delete all rows where col U (is_healthcare) == "false".
Deletes bottom-up to avoid row index shifting.
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

COL_IS_HEALTHCARE = 20  # U


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    print("=== Delete False Rows (is_healthcare = false) ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:U"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    to_delete = []
    for i, row in enumerate(data_rows):
        if cell(row, COL_IS_HEALTHCARE).lower() == "false":
            to_delete.append(i + 2)  # 1-indexed, +1 for header

    print(f"Rows to delete: {len(to_delete)}")

    if args.dry_run:
        for r in to_delete[:10]:
            print(f"  Row {r}: {data_rows[r-2][0] if data_rows[r-2] else '?'}")
        print("\n[DRY RUN] No changes.")
        return

    # Delete bottom-up to avoid index shifting
    delete_list = sorted(to_delete, reverse=True)
    requests_body = [
        {"deleteDimension": {"range": {
            "sheetId": sheet_gid, "dimension": "ROWS",
            "startIndex": r - 1, "endIndex": r,
        }}}
        for r in delete_list
    ]

    BATCH = 100
    for i in range(0, len(requests_body), BATCH):
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": requests_body[i:i + BATCH]},
        ).execute()
        done = min(i + BATCH, len(requests_body))
        print(f"  Deleted {done}/{len(requests_body)}")
        time.sleep(0.5)

    remaining = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A"
    ).execute()
    print(f"\n=== Done — {len(to_delete)} rows deleted ===")
    print(f"  Rows remaining: {len(remaining.get('values', [])) - 1}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
