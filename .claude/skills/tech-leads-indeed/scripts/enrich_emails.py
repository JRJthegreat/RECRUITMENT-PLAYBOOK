"""
Phase 3: Find emails for tech DMs via AnyMail Finder

For each lead with DM Name but no Email:
  - Split name → first_name, last_name
  - Extract domain from Company Website
  - POST to AnyMail Finder /v5.1/find-email/person
    Auth header: "Authorization: {API_KEY}" (NO "Bearer" prefix)
  - Accept email_status in ("valid", "risky")
  - Write email to sheet (or "not_found" so we don't retry forever)

Batches of 10, 5 parallel workers.
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

ANYMAILFINDER_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

MAX_WORKERS = 5
BATCH_SIZE = 10
SHEET_WRITE_DELAY = 1.5

# Column indices (matching pull_dataset.py HEADERS)
COL_COMPANY_NAME = 10    # K
COL_COMPANY_WEBSITE = 11 # L
COL_DM_NAME = 19         # T
COL_EMAIL = 22           # W


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service():
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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def split_name(full_name):
    name = full_name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0], parts[0]
    parts = name.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_domain(company_url):
    if not company_url:
        return None
    if "linkedin.com" in company_url:
        return None
    domain = company_url.strip()
    domain = re.sub(r'^https?://', '', domain)
    domain = re.sub(r'^www\.', '', domain)
    domain = domain.split('/')[0].split('?')[0]
    return domain if domain else None


def find_email_person(full_name, domain, company_name):
    """Find email via AnyMail Finder /v5.1/find-email/person."""
    first_name, last_name = split_name(full_name)

    headers = {"Authorization": ANYMAILFINDER_API_KEY, "Content-Type": "application/json"}
    body = {}
    if full_name:
        body["full_name"] = full_name
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name

    has_name = full_name or (first_name and last_name)
    has_company = domain or company_name
    if not has_name or not has_company:
        return {"email": None, "status": "missing_data"}

    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        if email and status in ("valid", "risky"):
            return {"email": email, "status": status}
        return {"email": None, "status": status or "not_found"}
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def process_lead(lead):
    domain = extract_domain(lead["company_website"])
    company_name = lead["company_name"]
    if not domain and not company_name:
        return {**lead, "email": None, "status": "missing_data"}
    result = find_email_person(lead["dm_name"], domain, company_name)
    return {**lead, **result}


def main():
    parser = argparse.ArgumentParser(description="Enrich emails via AnyMail Finder")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--limit", type=int, default=0, help="Max leads (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview without calling API")
    args = parser.parse_args()

    if not ANYMAILFINDER_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set in .env")
        return

    print("=== Enrich Emails (AnyMail Finder) ===\n")

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"  Using tab: '{tab_name}'")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AC"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("  No data rows found.")
        return

    leads = []
    for i, row in enumerate(all_rows[1:]):
        if args.limit > 0 and len(leads) >= args.limit:
            break

        dm_name = cell(row, COL_DM_NAME)
        email = cell(row, COL_EMAIL)

        if dm_name and not email:
            leads.append({
                "sheet_row": i + 2,
                "dm_name": dm_name,
                "company_name": cell(row, COL_COMPANY_NAME),
                "company_website": cell(row, COL_COMPANY_WEBSITE),
            })

    print(f"  {len(leads)} leads need email enrichment")
    if not leads:
        return

    if args.dry_run:
        for lead in leads[:20]:
            first, last = split_name(lead["dm_name"])
            domain = extract_domain(lead["company_website"])
            fallback = f" (no domain, fallback: {lead['company_name']})" if not domain else ""
            print(f"  Row {lead['sheet_row']}: {first} {last} @ {domain or '?'}{fallback}")
        if len(leads) > 20:
            print(f"  ... and {len(leads) - 20} more")
        return

    total_found = 0
    total_failed = 0
    num_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE

    for b in range(num_batches):
        batch = leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"  Batch {b + 1}/{num_batches}")

        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_lead, lead): lead for lead in batch}
            for future in as_completed(futures):
                results.append(future.result())

        updates = []
        for r in results:
            if r["email"]:
                status_note = f" [{r['status']}]" if r["status"] == "risky" else ""
                print(f"    Row {r['sheet_row']}: {r['dm_name']} → {r['email']}{status_note}")
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_EMAIL)}{r['sheet_row']}",
                    "values": [[r["email"]]],
                })
                total_found += 1
            else:
                print(f"    Row {r['sheet_row']}: {r['dm_name']} → NOT FOUND ({r['status']})")
                # Mark as not_found so re-runs skip this row
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_EMAIL)}{r['sheet_row']}",
                    "values": [["not_found"]],
                })
                total_failed += 1

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"  → Written {len(updates)} cells to sheet")

        time.sleep(SHEET_WRITE_DELAY)

    print(f"\n=== Done ===")
    print(f"  Emails found: {total_found}")
    print(f"  Not found:    {total_failed}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
