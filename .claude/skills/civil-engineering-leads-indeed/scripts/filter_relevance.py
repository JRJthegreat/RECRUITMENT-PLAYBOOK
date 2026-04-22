"""
Phase 1.7: Drop admin / sales rows from the Leads tab.

Indeed returns noise (BD Managers, HR Advisors, Cleaners, Account Managers)
that slip past a civil-engineering keyword search. This script drops titles
matching admin/sales patterns. Engineering-adjacent titles (Mechanical Eng,
Maintenance Eng, Field Service Eng, Product Designer, Draughtsman, etc.) are
kept — the lead pipeline targets engineering hiring generally, not just civil.

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
COL_JOB_TITLE = 1  # B

# Drop these title patterns — admin, sales, non-engineering ops.
DROP_PATTERNS = [
    r"\bbusiness\s+development\b",
    r"\b(key\s+)?account\s+(manager|executive|director|lead)\b",
    r"\bsales\s+(manager|executive|representative|director|consultant|engineer|coordinator|associate|lead)\b",
    r"\bhr\b", r"\bhuman\s+resources\b", r"\btalent\s+(acquisition|partner)\b",
    r"\brecruit(er|ing|ment)\b", r"\bpeople\s+(manager|partner|advisor|operations)\b",
    r"\bcleaner\b", r"\bcleaning\b",
    r"\bfacilities\s+(manager|coordinator|officer|assistant)\b",
    r"\bprocurement\b", r"\bbuyer\b", r"\bpurchasing\b",
    r"\bmarketing\b", r"\bbrand\s+manager\b",
    r"\bcustomer\s+(service|success|experience|support)\b",
    r"\badministrat(or|ive|ion)\b",
    r"\breceptionist\b", r"\boffice\s+manager\b",
    r"\bproject\s+coordinator\b",
    r"\bcontract\s+administrator\b",
    r"\baccounts?\s+(payable|receivable|assistant|clerk)\b",
    r"\bfinance\s+(manager|director|assistant|officer)\b", r"\baccountant\b",
    r"\bpayroll\b",
    r"\blegal\s+(counsel|advisor)\b", r"\bpara-?legal\b",
    r"\bexecutive\s+assistant\b", r"\bpersonal\s+assistant\b",
    r"\bcommercial\s+(manager|director|lead|officer)\b",
    r"\bdriver\b", r"\blabou?rer\b", r"\bwarehouse\b",
    r"\boperative\b",                 # plant operative, etc.
    r"\bstorekeeper\b", r"\bstore\s+person\b",
    r"\bteacher\b", r"\btutor\b", r"\blecturer\b",
    r"\bnurse\b", r"\bcarer\b",
]

DROP_RE = re.compile("|".join(DROP_PATTERNS), re.IGNORECASE)


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
    parser = argparse.ArgumentParser(description="Drop admin/sales rows from Leads tab")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually delete. Default: dry run.")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Filter Relevance ({mode}) ===")

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:B10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}\n")

    drops, keeps = [], []
    for i, row in enumerate(rows):
        sheet_row = i + 2
        title = row[COL_JOB_TITLE] if len(row) > COL_JOB_TITLE else ""
        if DROP_RE.search(title):
            drops.append((sheet_row, title))
        else:
            keeps.append(title)

    print(f"KEEP: {len(keeps)}")
    print(f"DROP: {len(drops)}\n")

    from collections import Counter
    drop_counter = Counter(t for _, t in drops)
    print("Top 30 titles being dropped:")
    for t, n in drop_counter.most_common(30):
        print(f"  {n:3d}  {t}")

    print("\nSample of titles KEPT (first 30 unique):")
    seen_k = set()
    for t in keeps:
        if t not in seen_k:
            seen_k.add(t)
            print(f"       {t}")
            if len(seen_k) >= 30:
                break

    if not args.apply:
        print("\n[DRY RUN] No changes. Re-run with --apply to delete.")
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
        print(f"  Deleted chunk {i // BATCH + 1}/{(len(reqs) + BATCH - 1) // BATCH}")

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
    ).execute()
    print(f"\nRows remaining: {len(result.get('values', []))}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
