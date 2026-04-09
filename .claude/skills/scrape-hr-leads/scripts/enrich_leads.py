"""
Phase 2: Find emails for decision makers via AnyMail Finder → Google Sheets

Two modes:
1. Rows WITH person_name (found by find_dm.py) → find-email/person endpoint
2. Rows WITHOUT person_name (LinkedIn scraper missed) → find-email/decision-maker endpoint
   which returns name + email + title + LinkedIn URL in one call
"""

import os
import sys
import json
import argparse
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

AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
MAX_WORKERS = 10

# Import rules from find_dm.py for DM category mapping
SENIOR_HR_TITLES = [
    "hr director", "director of hr", "director of human resources",
    "vp of people", "vp of hr", "vp human resources", "vp, people",
    "head of people", "head of hr", "head of human resources",
    "chief people officer", "chief human resources officer", "chro", "cpo",
    "svp people", "svp hr", "director of people", "director of talent",
]


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service(token_path):
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


def col_letter(idx):
    """Convert 0-based column index to sheet letter."""
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def split_name(full_name):
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_employee_count(count_str):
    if not count_str:
        return None
    s = str(count_str).strip().replace(",", "").replace("+", "")
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def get_dm_category(job_title, employee_count_str):
    """Map job title + company size to AMF decision_maker_category."""
    title_lower = (job_title or "").lower().strip()

    # If hiring a senior HR leader → target CEO
    for keyword in SENIOR_HR_TITLES:
        if keyword in title_lower:
            return ["ceo"]

    count = parse_employee_count(employee_count_str)

    # Small or unknown company → CEO
    if count is None or count < 200:
        return ["ceo"]

    # Everything else → HR decision maker
    return ["hr"]


def find_email_person(api_key, full_name, company_domain, company_name):
    """Find email for a known person via AMF person endpoint."""
    first_name, last_name = split_name(full_name)

    headers = {"Authorization": api_key, "Content-Type": "application/json"}
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


def find_dm_email(api_key, company_domain, company_name, dm_categories):
    """Find DM + email via AMF decision-maker endpoint. Returns name, email, title, linkedin."""
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"decision_maker_category": dm_categories}
    if company_domain:
        body["domain"] = company_domain
    if company_name:
        body["company_name"] = company_name

    if not company_domain and not company_name:
        return {"email": None, "status": "missing_data"}

    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        email = data.get("valid_email") or data.get("email")
        status = data.get("email_status", "unknown")
        result = {
            "email": email if email and status in ("valid", "risky") else None,
            "status": status or "not_found",
            "person_name": data.get("person_full_name", ""),
            "person_title": data.get("person_job_title", ""),
            "person_linkedin": data.get("person_linkedin_url", ""),
            "dm_category": data.get("decision_maker_category", ""),
        }
        return result
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def main():
    parser = argparse.ArgumentParser(description="Find emails via AnyMail Finder for HR leads")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process (0 = all)")
    parser.add_argument("--dm_only", action="store_true", help="Only process rows needing DM lookup (no person_name)")
    parser.add_argument("--email_only", action="store_true", help="Only process rows that already have person_name")
    args = parser.parse_args()

    api_key = os.getenv("ANYMAILFINDER_API_KEY")
    if not api_key:
        print("Error: ANYMAILFINDER_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Data"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    idx_person = col_idx("person_name")
    idx_result_title = col_idx("result_title")
    idx_linkedin = col_idx("linkedin_url")
    idx_email = col_idx("email")
    idx_company_url = col_idx("company_url")
    idx_company_name = col_idx("company name")
    idx_job_title = col_idx("job_title")
    idx_employee_count = col_idx("company_employee_count")
    idx_dm_confidence = col_idx("dm_confidence")

    missing = []
    for name, idx in [("email", idx_email), ("company_url", idx_company_url),
                       ("company name", idx_company_name)]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        sys.exit(1)

    # Collect rows into two groups
    email_rows = []  # Have person_name, need email
    dm_rows = []     # No person_name, need DM + email

    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        email = cell(idx_email)
        if email:
            continue  # Already has email

        person_name = cell(idx_person)
        company_url = cell(idx_company_url)
        company_name = cell(idx_company_name)

        # Skip if company_url is a LinkedIn URL (no domain for AMF)
        if "linkedin.com" in company_url:
            continue

        # Skip if no company info at all
        if not company_url and not company_name:
            continue

        if person_name:
            if not args.dm_only:
                email_rows.append({
                    "row_num": i,
                    "full_name": person_name,
                    "company_domain": company_url,
                    "company_name": company_name,
                })
        else:
            if not args.email_only:
                dm_rows.append({
                    "row_num": i,
                    "company_domain": company_url,
                    "company_name": company_name,
                    "job_title": cell(idx_job_title),
                    "employee_count": cell(idx_employee_count),
                })

        if args.limit and (len(email_rows) + len(dm_rows)) >= args.limit:
            break

    if not email_rows and not dm_rows:
        print("No rows need enrichment")
        sys.exit(0)

    print(f"\n  {len(email_rows)} rows with person_name → find email")
    print(f"  {len(dm_rows)} rows without person_name → find DM + email")

    BATCH_SIZE = 10
    email_found = 0
    dm_found = 0
    not_found = 0

    def write_updates_to_sheet(updates):
        """Write a list of row updates to the sheet."""
        if not updates:
            return
        batch = []
        for u in updates:
            for col_index, value in u["data"].items():
                if col_index is not None:
                    batch.append({
                        "range": f"{col_letter(col_index)}{u['row']}",
                        "values": [[value]],
                    })
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": batch},
        ).execute()

    # --- Mode 1: Find emails for known people (batches of 10) ---
    if email_rows:
        total_batches = (len(email_rows) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Finding emails for {len(email_rows)} known people ({total_batches} batches) ---\n")

        def enrich_person(row_data):
            result = find_email_person(
                api_key, row_data["full_name"],
                row_data["company_domain"], row_data["company_name"]
            )
            return row_data["row_num"], row_data["full_name"], result

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch = email_rows[batch_start:batch_start + BATCH_SIZE]
            batch_updates = []

            print(f"  Batch {batch_num + 1}/{total_batches}")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(enrich_person, r): r for r in batch}
                for future in as_completed(futures):
                    row_num, name, result = future.result()
                    if result["email"]:
                        batch_updates.append({
                            "row": row_num,
                            "data": {idx_email: result["email"]},
                        })
                        print(f"    Row {row_num}: {result['email']} — {name}")
                        email_found += 1
                    else:
                        print(f"    Row {row_num}: not found ({result['status']}) — {name}")
                        not_found += 1

            write_updates_to_sheet(batch_updates)
            print(f"    → Written to sheet. Running total: {email_found} found, {not_found} not found\n")

    # --- Mode 2: Find DMs + emails for unknown people (batches of 10) ---
    if dm_rows:
        total_batches = (len(dm_rows) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Finding DMs + emails for {len(dm_rows)} companies ({total_batches} batches) ---\n")

        def enrich_dm(row_data):
            categories = get_dm_category(row_data["job_title"], row_data["employee_count"])
            result = find_dm_email(
                api_key, row_data["company_domain"],
                row_data["company_name"], categories
            )
            return row_data["row_num"], row_data["company_name"], result

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch = dm_rows[batch_start:batch_start + BATCH_SIZE]
            batch_updates = []

            print(f"  Batch {batch_num + 1}/{total_batches}")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(enrich_dm, r): r for r in batch}
                for future in as_completed(futures):
                    row_num, company, result = future.result()
                    row_updates = {}

                    if result.get("person_name"):
                        row_updates[idx_person] = result["person_name"]
                        if idx_result_title is not None and result.get("person_title"):
                            row_updates[idx_result_title] = result["person_title"]
                        if idx_linkedin is not None and result.get("person_linkedin"):
                            row_updates[idx_linkedin] = result["person_linkedin"]
                        if idx_dm_confidence is not None:
                            row_updates[idx_dm_confidence] = "amf_dm"

                    if result.get("email"):
                        row_updates[idx_email] = result["email"]
                        dm_found += 1
                        print(f"    Row {row_num}: {result['person_name']} <{result['email']}> ({result.get('person_title', '')}) — {company}")
                    elif result.get("person_name"):
                        dm_found += 1
                        print(f"    Row {row_num}: {result['person_name']} (no email) — {company}")
                        not_found += 1
                    else:
                        print(f"    Row {row_num}: not found ({result['status']}) — {company}")
                        not_found += 1

                    if row_updates:
                        batch_updates.append({"row": row_num, "data": row_updates})

            write_updates_to_sheet(batch_updates)
            print(f"    → Written to sheet. Running total: {dm_found} DMs, {not_found} not found\n")

    # Summary
    print(f"\n{'='*50}")
    print(f"Phase 2 Complete")
    print(f"  Emails found (known people): {email_found}")
    print(f"  DMs found (AMF decision-maker): {dm_found}")
    print(f"  Not found: {not_found}")
    print(f"  Total processed: {email_found + dm_found + not_found}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
