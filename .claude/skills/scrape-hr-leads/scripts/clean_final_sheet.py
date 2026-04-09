"""
Clean Final Sheet — filter no-website rows + deduplicate by seniority

Reads "HR Leads - Apify Final", drops rows missing Company Website or
Company Name, then keeps only the most senior HR job title per company.
Writes result to a new "HR Leads - Clean Final" sheet.
"""

import os
import json
import time
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

SRC_SHEET_ID = "1jopIsvbAmhxoQmmKXAQTBp1zeujzwOfNAPBsNaCpWqA"
NEW_TITLE = "HR Leads - Clean Final"
TAB = "Leads"
BATCH = 10

# Column indices (0-based)
COL_JOB_TITLE = 1       # B
COL_COMPANY_NAME = 10   # K
COL_COMPANY_WEBSITE = 11  # L

# Seniority tiers — first match wins (highest score = most senior)
SENIORITY_TIERS = [
    (100, ["chief", "chro", "cpo"]),
    (90,  ["vp ", " vp", "vice president"]),
    (80,  ["director"]),
    (75,  ["head of"]),
    (65,  ["senior manager"]),
    (55,  ["manager"]),
    (40,  ["lead", "senior"]),
    (30,  ["specialist", "analyst", "coordinator", "hrbp"]),
    (20,  ["generalist", "associate", "administrator", "advisor"]),
    (10,  ["assistant", "junior", "entry"]),
]
DEFAULT_SCORE = 5


def get_google_service():
    with open(TOKEN_PATH) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(token_data, f)
    return build("sheets", "v4", credentials=creds)


def seniority_score(job_title):
    t = (job_title or "").lower()
    for score, keywords in SENIORITY_TIERS:
        if any(kw in t for kw in keywords):
            return score
    return DEFAULT_SCORE


def completeness(row):
    """Count non-empty cells — used as tiebreaker."""
    return sum(1 for cell in row if str(cell).strip())


def create_sheet(service, title):
    resp = service.spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId",
    ).execute()
    return resp["spreadsheetId"]


def setup_tab(service, sheet_id, header):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    default_gid = meta["sheets"][0]["properties"]["sheetId"]
    default_title = meta["sheets"][0]["properties"]["title"]

    if default_title != TAB:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": default_gid, "title": TAB},
                "fields": "title",
            }}]},
        ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{TAB}'!A1",
        valueInputOption="RAW",
        body={"values": [header]},
    ).execute()


def write_rows(service, sheet_id, rows):
    total = len(rows)
    for i in range(0, total, BATCH):
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{TAB}'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows[i:i + BATCH]},
        ).execute()
        print(f"  {min(i + BATCH, total)}/{total} rows written...", end="\r")
        time.sleep(1.5)
    print(f"  {total} rows written.          ")


def main():
    print("=== Clean Final Sheet ===\n")
    service = get_google_service()

    # ── Step 1: Read source sheet ────────────────────────────────────────
    print("[1/3] Reading source sheet...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SRC_SHEET_ID, range=f"'{TAB}'!A:AA"
    ).execute()
    all_rows = result.get("values", [])
    header = all_rows[0]
    data = all_rows[1:]
    print(f"  {len(data)} rows loaded")

    # ── Step 2: Filter + deduplicate ────────────────────────────────────
    print("\n[2/3] Filtering and deduplicating...")

    no_website = 0
    no_company = 0
    # company_name (lower) → best row so far
    best: dict[str, list] = {}

    for row in data:
        company = row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else ""
        website = row[COL_COMPANY_WEBSITE] if len(row) > COL_COMPANY_WEBSITE else ""

        if not company.strip():
            no_company += 1
            continue
        if not website.strip():
            no_website += 1
            continue

        key = company.strip().lower()
        score = seniority_score(row[COL_JOB_TITLE] if len(row) > COL_JOB_TITLE else "")

        if key not in best:
            best[key] = (score, completeness(row), row)
        else:
            prev_score, prev_complete, _ = best[key]
            if score > prev_score or (score == prev_score and completeness(row) > prev_complete):
                best[key] = (score, completeness(row), row)

    kept = [v[2] for v in best.values()]

    print(f"  Dropped — no company name: {no_company}")
    print(f"  Dropped — no website:      {no_website}")
    print(f"  Kept after dedup:          {len(kept)}")

    # ── Step 3: Write to new sheet ───────────────────────────────────────
    print(f"\n[3/3] Writing {len(kept)} rows to new sheet...")
    sid = create_sheet(service, NEW_TITLE)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    print(f"  Created: {sheet_url}")

    setup_tab(service, sid, header)
    write_rows(service, sid, kept)

    print(f"\n=== Done ===")
    print(f"Sheet: {sheet_url}")
    print(f"Rows:  {len(kept)}")


if __name__ == "__main__":
    main()
