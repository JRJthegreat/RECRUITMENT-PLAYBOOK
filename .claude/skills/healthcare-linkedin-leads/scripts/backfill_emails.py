"""
Backfill emails for rows that have a DM Name (col AA) but no Email (col AD).
Uses AnyMail Finder /find-email/person with name + domain (col U).

Resume-safe: skips rows where col AD is already populated.
Applies to both Multiple Openings and Single Opening tabs.

Usage:
  python3 -W ignore backfill_emails.py --sheet_url "URL"
"""

import os
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

COL_COMPANY = 12   # M
COL_WEBSITE  = 20  # U
COL_DM_NAME  = 26  # AA
COL_EMAIL    = 29  # AD

TABS = ["Multiple Openings", "Single Opening"]
BATCH_SIZE = 10


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_google_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    from google.oauth2.credentials import Credentials as C
    from google.auth.transport.requests import Request as R
    creds = C(token=td["token"], refresh_token=td["refresh_token"],
              token_uri=td["token_uri"], client_id=td["client_id"],
              client_secret=td["client_secret"],
              scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]))
    if creds.expired:
        creds.refresh(R())
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


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def extract_domain(website):
    w = (website or "").strip().lower()
    if not w:
        return ""
    if "://" not in w:
        w = "http://" + w
    net = urlparse(w).netloc
    return net[4:] if net.startswith("www.") else net


def find_email(name, domain):
    try:
        resp = requests.post(
            AMF_PERSON_URL,
            headers={"Authorization": AMF_KEY, "Content-Type": "application/json"},
            json={"full_name": name, "domain": domain},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        return email if email and status == "valid" else None
    except Exception:
        return None


def tab_exists(service, sheet_id, title):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def main():
    ap = argparse.ArgumentParser(description="Backfill emails for rows with DM name but no email")
    ap.add_argument("--sheet_url", required=True)
    args = ap.parse_args()

    if not AMF_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set")
        return

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    print("=== Backfill Emails (AMF /find-email/person) ===\n")
    total_found = total_miss = 0

    for tab in TABS:
        if not tab_exists(service, sheet_id, tab):
            continue
        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:AD10000"
        ).execute().get("values", [])

        todo = []
        for i, r in enumerate(rows):
            dm_name = cell(r, COL_DM_NAME)
            email = cell(r, COL_EMAIL)
            if dm_name and not email:
                domain = extract_domain(cell(r, COL_WEBSITE))
                if domain:
                    todo.append({"row": i + 2, "name": dm_name,
                                 "domain": domain, "company": cell(r, COL_COMPANY)})

        print(f"{tab}: {len(todo)} rows need email backfill")

        found = miss = 0
        updates = []
        for j, lead in enumerate(todo):
            email = find_email(lead["name"], lead["domain"])
            if email:
                found += 1
                print(f"  ✓ row {lead['row']:4d} {lead['name'][:28]:28s} -> {email}")
                updates.append({"range": f"'{tab}'!{col_letter(COL_EMAIL)}{lead['row']}",
                                "values": [[email]]})
            else:
                miss += 1
                print(f"  ✗ row {lead['row']:4d} {lead['name'][:28]:28s} -> not found")

            if (j + 1) % BATCH_SIZE == 0 or (j + 1) == len(todo):
                if updates:
                    for attempt in range(3):
                        try:
                            service.spreadsheets().values().batchUpdate(
                                spreadsheetId=sheet_id,
                                body={"valueInputOption": "RAW", "data": updates}
                            ).execute()
                            updates = []
                            break
                        except Exception as e:
                            if attempt < 2:
                                time.sleep(4)
                            else:
                                print(f"  [!] write failed: {e}")
                time.sleep(0.5)

        print(f"  -> found {found}, not found {miss}\n")
        total_found += found
        total_miss += miss

    print(f"=== Done === emails found: {total_found}, not found: {total_miss}")


if __name__ == "__main__":
    main()
