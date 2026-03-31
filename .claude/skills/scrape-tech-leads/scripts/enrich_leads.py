"""
Phase 2: Find emails for decision makers via AnyMail Finder → Google Sheets

Two modes:
1. Rows WITH person_name (found by find_dm.py) → find-email/person endpoint
2. Rows WITHOUT person_name (LinkedIn scraper missed) → find-email/decision-maker endpoint
   which returns name + email + title + LinkedIn URL in one call

Supports multiple tabs (Perm, Contract, custom names via --tab).
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

# Senior tech titles — hiring for these triggers CEO targeting
SENIOR_TECH_TITLES = [
    "cto", "chief technology officer", "chief technical officer",
    "vp of engineering", "vp engineering", "vice president of engineering",
    "head of engineering", "head of technology",
    "director of engineering", "engineering director",
    "chief architect", "vp of technology",
    "vp of data", "head of data", "chief data officer", "cdo",
    "chief information officer", "cio", "chief ai officer",
    "director of technology", "director of software engineering",
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


def get_dm_category(job_title, employee_count_str, tab_name):
    """Map job title + company size + tab to AMF decision_maker_category."""
    title_lower = (job_title or "").lower().strip()

    # Contract tab → always engineering (CTO/VP Eng)
    if "contract" in tab_name.lower():
        return ["engineering"]

    # Senior tech hire → CEO
    for keyword in SENIOR_TECH_TITLES:
        if keyword in title_lower:
            return ["ceo"]

    count = parse_employee_count(employee_count_str)

    # Small or unknown → CEO
    if count is None or count < 50:
        return ["ceo"]

    # 50+ → engineering (CTO/VP Eng)
    return ["engineering"]


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


def read_tab_data(service, sheet_id, tab_name):
    """Read all rows from a tab. Returns (headers, all_rows)."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return [], []
    return all_rows[0], all_rows


def main():
    parser = argparse.ArgumentParser(description="Find emails via AnyMail Finder for tech leads")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--tab", default="Data",
                        help="Tab to process (default: Data), or comma-separated names")
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

    # Determine which tabs to process
    tabs_to_process = [t.strip() for t in args.tab.split(",") if t.strip()]

    # Collect rows from all tabs
    all_email_rows = []  # Have person_name, need email
    all_dm_rows = []     # No person_name, need DM + email
    tab_indices = {}     # tab_name → column indices

    total_limit_remaining = args.limit if args.limit else None

    for tab_name in tabs_to_process:
        print(f"\nReading '{tab_name}' tab...")
        headers, all_rows = read_tab_data(service, sheet_id, tab_name)
        if not headers:
            print(f"  No data found")
            continue

        def col_idx(name):
            try:
                return headers.index(name)
            except ValueError:
                return None

        indices = {
            "person_name": col_idx("person_name"),
            "result_title": col_idx("result_title"),
            "linkedin_url": col_idx("linkedin_url"),
            "email": col_idx("email"),
            "company_url": col_idx("company_url"),
            "company_name": col_idx("company name"),
            "job_title": col_idx("job_title"),
            "employee_count": col_idx("company_employee_count"),
            "dm_confidence": col_idx("dm_confidence"),
        }

        missing = []
        for name in ["email", "company_name"]:
            if indices[name] is None:
                missing.append(name)
        if missing:
            print(f"  Missing columns: {', '.join(missing)}")
            continue

        tab_indices[tab_name] = indices
        tab_email_count = 0
        tab_dm_count = 0

        for i, row in enumerate(all_rows[1:], start=2):
            if total_limit_remaining is not None and (len(all_email_rows) + len(all_dm_rows)) >= args.limit:
                break

            def cell(idx):
                if idx is None:
                    return ""
                return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

            email = cell(indices["email"])
            if email:
                continue  # Already has email

            person_name = cell(indices["person_name"])
            company_url = cell(indices["company_url"])
            company_name = cell(indices["company_name"])

            # Extract domain from company_url — skip LinkedIn URLs for domain
            company_domain = company_url if company_url and "linkedin.com" not in company_url else ""

            # Skip if no company info at all
            if not company_domain and not company_name:
                continue

            if person_name:
                if not args.dm_only:
                    all_email_rows.append({
                        "tab": tab_name,
                        "row_num": i,
                        "full_name": person_name,
                        "company_domain": company_domain,
                        "company_name": company_name,
                    })
                    tab_email_count += 1
            else:
                if not args.email_only:
                    all_dm_rows.append({
                        "tab": tab_name,
                        "row_num": i,
                        "company_domain": company_domain,
                        "company_name": company_name,
                        "job_title": cell(indices["job_title"]) if indices["job_title"] is not None else "",
                        "employee_count": cell(indices["employee_count"]) if indices["employee_count"] is not None else "",
                    })
                    tab_dm_count += 1

        print(f"  {tab_email_count} rows with person_name (need email)")
        print(f"  {tab_dm_count} rows without person_name (need DM + email)")

    if not all_email_rows and not all_dm_rows:
        print("\nNo rows need enrichment")
        sys.exit(0)

    print(f"\nTotal: {len(all_email_rows)} email lookups + {len(all_dm_rows)} DM lookups")

    BATCH_SIZE = 10
    email_found = 0
    dm_found = 0
    not_found = 0

    def write_updates_to_sheet(updates, tab_name):
        """Write a list of row updates to the sheet for a specific tab."""
        if not updates:
            return
        batch = []
        for u in updates:
            for col_index, value in u["data"].items():
                if col_index is not None:
                    batch.append({
                        "range": f"'{tab_name}'!{col_letter(col_index)}{u['row']}",
                        "values": [[value]],
                    })
        if batch:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": batch},
            ).execute()

    # --- Mode 1: Find emails for known people (batches of 10) ---
    if all_email_rows:
        total_batches = (len(all_email_rows) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Finding emails for {len(all_email_rows)} known people ({total_batches} batches) ---\n")

        def enrich_person(row_data):
            result = find_email_person(
                api_key, row_data["full_name"],
                row_data["company_domain"], row_data["company_name"]
            )
            return row_data, result

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch = all_email_rows[batch_start:batch_start + BATCH_SIZE]
            # Group updates by tab
            tab_updates = {}  # tab_name → list of updates

            print(f"  Batch {batch_num + 1}/{total_batches}")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(enrich_person, r): r for r in batch}
                for future in as_completed(futures):
                    row_data, result = future.result()
                    tab = row_data["tab"]
                    indices = tab_indices[tab]

                    if result["email"]:
                        if tab not in tab_updates:
                            tab_updates[tab] = []
                        tab_updates[tab].append({
                            "row": row_data["row_num"],
                            "data": {indices["email"]: result["email"]},
                        })
                        print(f"    [{tab}] Row {row_data['row_num']}: {result['email']} — {row_data['full_name']}")
                        email_found += 1
                    else:
                        # Mark as not_found so we don't retry on next run
                        if tab not in tab_updates:
                            tab_updates[tab] = []
                        tab_updates[tab].append({
                            "row": row_data["row_num"],
                            "data": {indices["email"]: "not_found"},
                        })
                        print(f"    [{tab}] Row {row_data['row_num']}: not found ({result['status']}) — {row_data['full_name']}")
                        not_found += 1

            for tab, updates in tab_updates.items():
                write_updates_to_sheet(updates, tab)
            print(f"    → Written to sheet. Running total: {email_found} found, {not_found} not found\n")

    # --- Mode 2: Find DMs + emails for unknown people (batches of 10) ---
    if all_dm_rows:
        total_batches = (len(all_dm_rows) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Finding DMs + emails for {len(all_dm_rows)} companies ({total_batches} batches) ---\n")

        def enrich_dm(row_data):
            categories = get_dm_category(
                row_data["job_title"], row_data["employee_count"], row_data["tab"]
            )
            result = find_dm_email(
                api_key, row_data["company_domain"],
                row_data["company_name"], categories
            )
            return row_data, result

        for batch_num in range(total_batches):
            batch_start = batch_num * BATCH_SIZE
            batch = all_dm_rows[batch_start:batch_start + BATCH_SIZE]
            tab_updates = {}

            print(f"  Batch {batch_num + 1}/{total_batches}")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(enrich_dm, r): r for r in batch}
                for future in as_completed(futures):
                    row_data, result = future.result()
                    tab = row_data["tab"]
                    indices = tab_indices[tab]
                    row_updates = {}

                    if result.get("person_name"):
                        if indices["person_name"] is not None:
                            row_updates[indices["person_name"]] = result["person_name"]
                        if indices["result_title"] is not None and result.get("person_title"):
                            row_updates[indices["result_title"]] = result["person_title"]
                        if indices["linkedin_url"] is not None and result.get("person_linkedin"):
                            row_updates[indices["linkedin_url"]] = result["person_linkedin"]
                        if indices["dm_confidence"] is not None:
                            row_updates[indices["dm_confidence"]] = "amf_dm"

                    if result.get("email"):
                        if indices["email"] is not None:
                            row_updates[indices["email"]] = result["email"]
                        dm_found += 1
                        print(f"    [{tab}] Row {row_data['row_num']}: {result['person_name']} <{result['email']}> ({result.get('person_title', '')}) — {row_data['company_name']}")
                    elif result.get("person_name"):
                        dm_found += 1
                        print(f"    [{tab}] Row {row_data['row_num']}: {result['person_name']} (no email) — {row_data['company_name']}")
                        not_found += 1
                    else:
                        print(f"    [{tab}] Row {row_data['row_num']}: not found ({result['status']}) — {row_data['company_name']}")
                        not_found += 1

                    if row_updates:
                        if tab not in tab_updates:
                            tab_updates[tab] = []
                        tab_updates[tab].append({"row": row_data["row_num"], "data": row_updates})

            for tab, updates in tab_updates.items():
                write_updates_to_sheet(updates, tab)
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
