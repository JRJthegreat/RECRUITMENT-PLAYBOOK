"""
Phase 1: Pull Apify Indeed (civil engineering) dataset → Google Sheet

Filter applied at ingestion:
  - Company size ≤ 500: skip mid/large firms (locked under RPO/PSL contracts, unlikely to engage recruiters)

NO Perm/Contract filter — keeps all jobtypes (single template covers both downstream).

Column layout (29 cols):
  Job Info    A-J: Job_Id, Job Title, Job Type, Occupations, Date Published,
                   Salary Min, Salary Max, Salary Period, Apply URL, Job Description
  Company     K-Q: Company Name, Company Website, Company Size, Revenue,
                   CEO Name, Company Description, Benefits
  Location    R-S: City, State
  Outreach    T-AA: DM Name, DM Title, LinkedIn URL, Email,
                    First Name, Last Name, Email Body, Added to Instantly
  Civil extras AB-AC: template_variant, cleaned_role
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2/datasets"
APIFY_PAGE_SIZE = 1000

BATCH_SIZE = 10
TAB_NAME = "Leads"
MAX_EMPLOYEES = 500

HEADERS = [
    # Job Info (A-J)
    "Job_Id", "Job Title", "Job Type", "Occupations", "Date Published",
    "Salary Min", "Salary Max", "Salary Period", "Apply URL", "Job Description",
    # Company (K-Q)
    "Company Name", "Company Website", "Company Size", "Revenue",
    "CEO Name", "Company Description", "Benefits",
    # Location (R-S)
    "City", "State",
    # Outreach (T-AA) — blank, filled by downstream phases
    "DM Name", "DM Title", "LinkedIn URL", "Email",
    "First Name", "Last Name", "Email Body", "Added to Instantly",
    # Civil extras (AB-AC)
    "template_variant", "cleaned_role",
]


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


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


def create_sheet(service, title):
    resp = service.spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId",
    ).execute()
    sheet_id = resp["spreadsheetId"]
    print(f"  Created: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    return sheet_id


def setup_tab(service, sheet_id):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    default_sheet_id = meta["sheets"][0]["properties"]["sheetId"]
    default_title = meta["sheets"][0]["properties"]["title"]

    if default_title != TAB_NAME:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": default_sheet_id, "title": TAB_NAME},
                "fields": "title",
            }}]},
        ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{TAB_NAME}'!A1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    print(f"  Headers written ({len(HEADERS)} columns)")


def fetch_dataset(dataset_id):
    all_items = []
    offset = 0

    while True:
        resp = requests.get(
            f"{APIFY_BASE}/{dataset_id}/items",
            params={"token": APIFY_API_TOKEN, "format": "json",
                    "limit": APIFY_PAGE_SIZE, "offset": offset},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  ERROR (offset={offset}): HTTP {resp.status_code}")
            break

        items = resp.json()
        if not items:
            break

        all_items.extend(items)
        print(f"  Fetched {len(all_items)}...", end="\r")

        if len(items) < APIFY_PAGE_SIZE:
            break
        offset += APIFY_PAGE_SIZE

    print(f"  Fetched {len(all_items)} total items    ")
    return all_items


def fmt_salary(val):
    if val is None:
        return ""
    try:
        return str(int(round(float(val))))
    except (ValueError, TypeError):
        return str(val)


def parse_size_lower_bound(size_str):
    """Indeed returns ranges like '11 to 50', '201 to 500', '1,001 to 5,000', '10,000+'.
    Returns the lower bound as int, or None if unparseable."""
    if not size_str:
        return None
    s = str(size_str).strip().replace(",", "").replace("+", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    if not s:
        return None
    parts = s.split("-")
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def map_to_row(item):
    emp = item.get("employer") or {}
    loc = item.get("location") or {}
    sal = item.get("baseSalary") or {}
    desc_obj = item.get("description") or {}
    job_types = item.get("jobTypes") or {}
    occupations = item.get("occupations") or {}
    benefits = item.get("benefits") or {}

    job_type = ", ".join(v for v in job_types.values() if v) if isinstance(job_types, dict) else str(job_types)
    occ_str = ", ".join(v for v in occupations.values() if v) if isinstance(occupations, dict) else str(occupations)
    date_raw = item.get("datePublished") or ""
    date_str = date_raw[:10] if date_raw else ""
    sal_min = fmt_salary(sal.get("min"))
    sal_max = fmt_salary(sal.get("max"))
    sal_unit = (sal.get("unitOfWork") or "").upper()
    description = (desc_obj.get("text") or "") if isinstance(desc_obj, dict) else str(desc_obj)
    benefits_str = ", ".join(list(benefits.values())[:8]) if isinstance(benefits, dict) else str(benefits)
    employees_count_raw = emp.get("employeesCount", "")
    employees_count = employees_count_raw if isinstance(employees_count_raw, str) else str(employees_count_raw or "")

    return [
        # Job Info (A-J)
        item.get("key", ""),
        item.get("title", ""),
        job_type,
        occ_str,
        date_str,
        sal_min,
        sal_max,
        sal_unit,
        item.get("jobUrl", ""),
        description,
        # Company (K-Q)
        emp.get("name", ""),
        emp.get("corporateWebsite", ""),
        employees_count,
        emp.get("revenue", ""),
        emp.get("ceoName", ""),
        emp.get("briefDescription", ""),
        benefits_str,
        # Location (R-S)
        loc.get("city", ""),
        loc.get("admin1Code", ""),
        # Outreach (T-AA) — blank
        "", "", "", "", "", "", "", "",
        # Civil extras (AB-AC) — blank
        "", "",
    ]


def write_rows(service, sheet_id, rows):
    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{TAB_NAME}'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": batch},
        ).execute()
        print(f"  Written {min(i + BATCH_SIZE, total)}/{total} rows...", end="\r")
        time.sleep(1.5)
    print(f"  Written {total} rows total.          ")


def main():
    parser = argparse.ArgumentParser(description="Pull Apify Indeed civil engineering dataset → Google Sheet")
    parser.add_argument("--dataset_id", required=True, help="Apify dataset ID")
    parser.add_argument("--sheet_url", help="Existing Google Sheet URL to append to")
    parser.add_argument("--sheet_title", default="Civil Engineering Indeed Leads", help="Title for new sheet (ignored if --sheet_url)")
    parser.add_argument("--limit", type=int, default=0, help="Max items to pull post-filtering (0 = all)")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Pull Apify Civil Engineering Indeed Dataset → Google Sheet ===\n")

    service = get_google_service()

    if args.sheet_url:
        sheet_id = get_sheet_id_from_url(args.sheet_url)
        print(f"  Using existing sheet: {sheet_id}")
    else:
        print("[1/3] Creating Google Sheet...")
        sheet_id = create_sheet(service, args.sheet_title)
        setup_tab(service, sheet_id)

    print(f"\n[2/3] Fetching dataset {args.dataset_id}...")
    items = fetch_dataset(args.dataset_id)

    print(f"\n[3/3] Filtering and mapping {len(items)} items...")
    skipped_no_company = 0
    skipped_too_big = 0
    rows = []

    for item in items:
        emp = item.get("employer") or {}
        company_name = (emp.get("name") or "").strip()
        if not company_name:
            skipped_no_company += 1
            continue

        # Size filter (skip companies > MAX_EMPLOYEES)
        size_lower = parse_size_lower_bound(emp.get("employeesCount", ""))
        if size_lower is not None and size_lower > MAX_EMPLOYEES:
            skipped_too_big += 1
            continue

        rows.append(map_to_row(item))

        if args.limit > 0 and len(rows) >= args.limit:
            break

    print(f"  Skipped (no company name): {skipped_no_company}")
    print(f"  Skipped (>{MAX_EMPLOYEES} employees): {skipped_too_big}")
    print(f"  Kept: {len(rows)}")

    if rows:
        print(f"\nWriting {len(rows)} rows...")
        write_rows(service, sheet_id, rows)
    else:
        print("  No rows to write.")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"\n=== Done ===")
    print(f"Sheet:  {sheet_url}")
    print(f"Rows:   {len(rows)}")


if __name__ == "__main__":
    main()
