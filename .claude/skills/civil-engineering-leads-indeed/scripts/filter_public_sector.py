"""
Phase 1.85: Drop public-sector rows (councils, NHS, police, universities,
housing trusts, gov departments, regulated water utilities).

These go through procurement frameworks / PSLs and will not engage on cold
outreach. Run after filter_relevance.py, before find_dm.py.

Dry-run by default. Re-run with --apply to delete.
"""

import os
import re
import json
import argparse
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
TAB_NAME = "Leads"
COL_COMPANY = 10  # K

PUBLIC_SECTOR = re.compile(
    r"\bcouncil\b|\bborough\b|\bcity\s+region\b|\bcombined\s+authority\b|"
    r"\bnhs\b|\buniversity\b|\bcollege\b|\bacademy\s+trust\b|"
    r"\bpolice\b|\bconstabulary\b|\bfire\s+(service|rescue|brigade)\b|"
    r"\bministry\s+of\b|\bdepartment\s+(of|for)\b|\bhmrc\b|"
    r"\bnational\s+highways\b|\bhighways\s+england\b|\bnetwork\s+rail\b|"
    r"\bwelsh\s+water\b|\bthames\s+water\b|\bsevern\s+trent\b|"
    r"\bunited\s+utilities\b|\byorkshire\s+water\b|\bdwr\s+cymru\b|"
    r"\btransport\s+for\s+london\b|\btfl\b|\broyal\s+borough\b|"
    r"\bcrown\s+commercial\b|\bhousing\s+association\b|"
    r"\bhousing\s+trust\b|\bwildlife\s+trust\b|\bcare\s+trust\b",
    re.IGNORECASE,
)


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"], client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    return build("sheets", "v4", credentials=creds)


def get_tab_sheet_id(service, spreadsheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Tab {tab_name!r} not found")


def main():
    parser = argparse.ArgumentParser(description="Drop public-sector rows")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Filter Public Sector ({mode}) ===\n")

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:K10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    drops = []
    for i, row in enumerate(rows):
        sheet_row = i + 2
        company = row[COL_COMPANY] if len(row) > COL_COMPANY else ""
        if PUBLIC_SECTOR.search(company):
            drops.append((sheet_row, company))

    print(f"Public-sector matches: {len(drops)}\n")
    for _, c in drops:
        print(f"  {c}")

    if not args.apply:
        print("\n[DRY RUN] Re-run with --apply to delete.")
        return

    to_delete = sorted([r for r, _ in drops], reverse=True)
    print(f"\nDeleting {len(to_delete)} rows (bottom-up)...")
    reqs = [
        {"deleteDimension": {"range": {
            "sheetId": tab_sheet_id, "dimension": "ROWS",
            "startIndex": r - 1, "endIndex": r,
        }}}
        for r in to_delete
    ]
    BATCH = 100
    for i in range(0, len(reqs), BATCH):
        chunk = reqs[i:i + BATCH]
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": chunk}
        ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
    ).execute()
    print(f"Rows remaining: {len(result.get('values', []))}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
