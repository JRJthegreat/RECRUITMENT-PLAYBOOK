"""
Phase 2: Find emails for decision makers via AnyMail Finder → Google Sheets

Reads person_name + company_url from the sheet, calls AnyMail Finder,
and writes the email back. Skips rows that already have emails.
"""

import os
import sys
import json
import argparse
import time
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load .env from the skill's parent .claude directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

AMF_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
AMF_BULK_URL = "https://api.anymailfinder.com/v5.1/bulk/json"
BULK_THRESHOLD = 200
MAX_WORKERS = 10


def get_sheet_id_from_url(url):
    """Extract spreadsheet ID from a Google Sheets URL."""
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service(token_path):
    """Build Google Sheets service using existing OAuth token."""
    with open(token_path) as f:
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
        with open(token_path, "w") as f:
            json.dump(token_data, f)

    return build("sheets", "v4", credentials=creds)


def split_name(full_name):
    """Split a full name into first and last name."""
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def find_email_single(api_key, full_name, company_domain, company_name):
    """Query AnyMail Finder for a single person's email."""
    first_name, last_name = split_name(full_name)

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    body = {}
    if full_name:
        body["full_name"] = full_name
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if company_domain:
        body["domain"] = company_domain
    if company_name:
        body["company_name"] = company_name

    has_name = full_name or (first_name and last_name)
    has_company = company_domain or company_name
    if not has_name or not has_company:
        return None, "missing_data"

    try:
        resp = requests.post(AMF_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        email = data.get("email")
        status = data.get("email_status", "unknown")
        if email and status in ("valid", "risky"):
            return email, status
        return None, status or "not_found"
    except requests.exceptions.HTTPError as e:
        return None, f"http_{e.response.status_code}"
    except Exception as e:
        return None, f"error"


def find_email_bulk(api_key, rows_data):
    """Use AnyMail Finder bulk API for large batches. Returns list of (email, status) tuples."""
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }

    # Build bulk data table
    table = [["first_name", "last_name", "full_name", "domain", "company_name"]]
    for row in rows_data:
        first, last = split_name(row["full_name"])
        table.append([first, last, row["full_name"], row["company_domain"], row["company_name"]])

    body = {
        "data": table,
        "first_name_field_index": 0,
        "last_name_field_index": 1,
        "full_name_field_index": 2,
        "domain_field_index": 3,
        "company_name_field_index": 4,
        "file_name": f"hr_leads_{time.strftime('%Y%m%d_%H%M%S')}",
    }

    # Create bulk search
    try:
        resp = requests.post(AMF_BULK_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        search_id = resp.json().get("id")
        if not search_id:
            print("  Bulk API: no search ID returned")
            return None
        print(f"  Bulk search created: {search_id}")
    except Exception as e:
        print(f"  Bulk API error: {e}")
        return None

    # Poll until complete
    poll_url = f"https://api.anymailfinder.com/v5.1/bulk/{search_id}"
    while True:
        try:
            resp = requests.get(poll_url, headers={"Authorization": api_key}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            progress = data.get("progress", {})
            processed = progress.get("processed", 0)
            total = progress.get("total", 0)

            if status == "completed":
                print(f"  Bulk search completed ({processed}/{total})")
                break
            elif status == "failed":
                print(f"  Bulk search failed")
                return None
            else:
                print(f"  Bulk status: {status} ({processed}/{total})...")
                time.sleep(10)
        except Exception as e:
            print(f"  Poll error: {e}")
            return None

    # Download results
    dl_url = f"https://api.anymailfinder.com/v5.1/bulk/{search_id}/download"
    try:
        resp = requests.get(dl_url, headers={"Authorization": api_key}, timeout=60)
        resp.raise_for_status()
        results = resp.json().get("data", [])
    except Exception as e:
        print(f"  Download error: {e}")
        return None

    # Parse results (skip header row)
    email_results = []
    for row in results[1:]:
        email = row[5] if len(row) > 5 else None
        email_status = row[6] if len(row) > 6 else None
        if email and email_status in ("valid", "risky"):
            email_results.append((email, email_status))
        else:
            email_results.append((None, email_status or "not_found"))

    return email_results


def main():
    parser = argparse.ArgumentParser(description="Find emails via AnyMail Finder for HR leads")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    args = parser.parse_args()

    api_key = os.getenv("ANYMAILFINDER_API_KEY")
    if not api_key:
        print("Error: ANYMAILFINDER_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Read all data
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Sheet1"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    # Find column indices dynamically
    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    idx_person = col_idx("person_name")
    idx_email = col_idx("email")
    idx_company_url = col_idx("company_url")
    idx_company_name = col_idx("company name")

    missing = []
    for name, idx in [("person_name", idx_person), ("email", idx_email),
                       ("company_url", idx_company_url), ("company name", idx_company_name)]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        print(f"Available: {headers}")
        sys.exit(1)

    # Collect rows needing enrichment
    rows_to_enrich = []
    for i, row in enumerate(all_rows[1:], start=2):  # row 2 = first data row
        def cell(idx):
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        person_name = cell(idx_person)
        email = cell(idx_email)
        company_url = cell(idx_company_url)
        company_name = cell(idx_company_name)

        # Skip if already has email or no person name
        if email or not person_name:
            continue

        # Skip if company_url is a linkedin URL (no domain for AMF)
        if "linkedin.com" in company_url:
            print(f"  Row {i}: Skipping {person_name} — no company domain (LinkedIn URL)")
            continue

        rows_to_enrich.append({
            "row_num": i,
            "full_name": person_name,
            "company_domain": company_url,
            "company_name": company_name,
        })

    if not rows_to_enrich:
        print("No rows need email enrichment")
        sys.exit(0)

    print(f"\nEnriching {len(rows_to_enrich)} leads...\n")

    # Choose strategy based on count
    updates = []
    found = 0
    not_found = 0

    if len(rows_to_enrich) >= BULK_THRESHOLD:
        print(f"Using bulk API for {len(rows_to_enrich)} rows...")
        bulk_results = find_email_bulk(api_key, rows_to_enrich)

        if bulk_results is None:
            print("Bulk API failed, falling back to concurrent...")
            bulk_results = None  # will fall through to concurrent below
        else:
            for i, (email, status) in enumerate(bulk_results):
                if i >= len(rows_to_enrich):
                    break
                row = rows_to_enrich[i]
                if email:
                    updates.append({"row": row["row_num"], "email": email})
                    print(f"  Row {row['row_num']}: {email}")
                    found += 1
                else:
                    print(f"  Row {row['row_num']}: not found ({status}) — {row['full_name']}")
                    not_found += 1

    if len(rows_to_enrich) < BULK_THRESHOLD or (len(rows_to_enrich) >= BULK_THRESHOLD and not updates and not not_found):
        # Concurrent single lookups
        print(f"Using concurrent API ({MAX_WORKERS} workers)...")

        def enrich_one(row_data):
            email, status = find_email_single(
                api_key, row_data["full_name"],
                row_data["company_domain"], row_data["company_name"]
            )
            return row_data["row_num"], row_data["full_name"], email, status

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(enrich_one, r): r for r in rows_to_enrich}
            for future in as_completed(futures):
                row_num, name, email, status = future.result()
                if email:
                    updates.append({"row": row_num, "email": email})
                    print(f"  Row {row_num}: {email}")
                    found += 1
                else:
                    print(f"  Row {row_num}: not found ({status}) — {name}")
                    not_found += 1

    # Batch update sheet
    if updates:
        print(f"\nUpdating {len(updates)} emails in sheet...")
        # Email is in column E (index 4) → column letter E
        email_col_letter = chr(65 + idx_email)  # Convert index to letter
        batch = [{
            "range": f"{email_col_letter}{u['row']}",
            "values": [[u["email"]]]
        } for u in updates]

        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": batch},
        ).execute()

    # Summary
    print(f"\n{'='*50}")
    print(f"Phase 2 Complete")
    print(f"  Emails found: {found}")
    print(f"  Not found: {not_found}")
    print(f"  Total processed: {found + not_found}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
