"""
Fix DM conflicts: where the DM title is at the same non-senior HR level as the
job being filled.

If a company is hiring an HR Manager, the DM should NOT be another HR Manager —
they can't be the buyer (they're literally being hired). Replace with CEO name
from col O (Indeed's ceoName field) if available, or clear T/U/V/W for a fresh
Phase 2 search.

Dry-run by default; --apply to execute changes.

After running with --apply:
  • Rows replaced from col O → run Phase 3 --person_only to enrich emails
  • Rows cleared (no col O) → run Phase 2 first, then Phase 3 --person_only
"""

import os
import re
import json
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

TAB_NAME = "Leads"
COL_JOB_TITLE = 1         # B
COL_CEO_NAME = 14         # O
COL_DM_NAME = 19          # T
COL_DM_TITLE = 20         # U
COL_LINKEDIN_URL = 21     # V
COL_EMAIL = 22            # W

SENIOR_KEYWORDS = (
    "vice president", "svp", "evp",
    "director", "head of", "head,",
    "chro", "cpo", "chief people", "chief human resources", "chief talent", "chief hr",
    "founder", "ceo", "president", "owner", "co-founder", "cofounder",
)

HR_KEYWORDS = (
    "hr", "human resource", "people", "talent", "recruit",
    "payroll", "benefits", "employee relation", "workforce", "hris",
    "learning", "dei", "diversity",
)

_INVALID_CEO_VALUES = {"n/a", "na", "none", "unknown", "-", ""}


def is_senior(title):
    t = (title or "").lower().strip()
    if re.search(r"\bvp\b", t):  # catches "VP, Talent", "Senior VP", "EVP/VP" etc.
        return True
    return any(kw in t for kw in SENIOR_KEYWORDS)


def valid_ceo_name(name):
    return name.strip().lower() not in _INVALID_CEO_VALUES


def is_hr_related(title):
    t = (title or "").lower()
    return any(kw in t for kw in HR_KEYWORDS)


def is_conflicting_dm(job_title, dm_title):
    """True if DM is a non-senior HR person filling the same role that's being hired."""
    if not dm_title:
        return False
    if is_senior(dm_title):
        return False  # VP / Director / C-suite DM is fine
    if not is_hr_related(dm_title):
        return False  # Non-HR DM (e.g. ops mgr) — different issue, not handled here
    if is_senior(job_title):
        return False  # Senior HR job → CEO is correct DM, not this case
    return True  # Both non-senior HR → conflict


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


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


def main():
    ap = argparse.ArgumentParser(
        description="Fix rows where DM title = same level as job being hired"
    )
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--apply", action="store_true", help="Write fixes to sheet (default: dry run)")
    args = ap.parse_args()

    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Fix DM Conflicts ({mode}) ===\n")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A:W"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"Total rows: {len(rows)}")

    ceo_filled = []    # will replace DM with CEO from col O
    cleared = []       # no col O → clear for re-search

    for i, row in enumerate(rows):
        sr = i + 2
        job_title = cell(row, COL_JOB_TITLE)
        ceo_name = cell(row, COL_CEO_NAME)
        dm_name = cell(row, COL_DM_NAME)
        dm_title = cell(row, COL_DM_TITLE)

        if not dm_name:
            continue
        if not is_conflicting_dm(job_title, dm_title):
            continue

        entry = {
            "row": sr,
            "job_title": job_title,
            "old_dm": dm_name,
            "old_title": dm_title,
            "ceo_name": ceo_name,
        }
        if ceo_name and valid_ceo_name(ceo_name):
            ceo_filled.append(entry)
        else:
            cleared.append(entry)

    total = len(ceo_filled) + len(cleared)
    print(f"\nConflicting DMs found: {total}")
    print(f"  → Replace with CEO from col O : {len(ceo_filled)}")
    print(f"  → Clear for re-search (no CEO): {len(cleared)}")

    if ceo_filled:
        print("\nReplacements (first 25):")
        for c in ceo_filled[:25]:
            print(f"  row {c['row']:4d}  {c['old_dm']!r}  ({c['old_title']!r})")
            print(f"           → CEO: {c['ceo_name']!r}   job: {c['job_title']!r}")

    if cleared:
        print("\nClears (first 25):")
        for c in cleared[:25]:
            print(f"  row {c['row']:4d}  {c['old_dm']!r}  ({c['old_title']!r})")
            print(f"           job: {c['job_title']!r}")

    if not args.apply:
        print("\n[DRY RUN] No changes. Re-run with --apply.")
        return

    if not total:
        print("\nNothing to fix.")
        return

    updates = []
    for c in ceo_filled:
        sr = c["row"]
        updates += [
            {"range": f"'{TAB_NAME}'!{col_letter(COL_DM_NAME)}{sr}",    "values": [[c["ceo_name"]]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_DM_TITLE)}{sr}",   "values": [[""]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN_URL)}{sr}","values": [[""]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}",       "values": [[""]]},
        ]
    for c in cleared:
        sr = c["row"]
        updates += [
            {"range": f"'{TAB_NAME}'!{col_letter(COL_DM_NAME)}{sr}",    "values": [[""]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_DM_TITLE)}{sr}",   "values": [[""]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN_URL)}{sr}","values": [[""]]},
            {"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}",       "values": [[""]]},
        ]

    CHUNK = 200
    for i in range(0, len(updates), CHUNK):
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": updates[i:i + CHUNK]},
        ).execute()

    print(f"\nDone: {len(ceo_filled)} CEO replacements, {len(cleared)} clears.")
    print("\nNext steps:")
    if cleared:
        print("  1. python3 find_dm.py --sheet_url ... --limit 0  (Phase 2 re-search)")
    print("  2. python3 enrich_emails.py --sheet_url ... --person_only  (Phase 3 email only)")
    print("=== Done ===")


if __name__ == "__main__":
    main()
