"""
Fetch Apify Indeed datasets → filter HR/Ops jobs → write to new Google Sheet

Column layout (27 cols):
  Job Info    A-J: Job_Id, Job Title, Job Type, Occupations, Date Published,
                   Salary Min, Salary Max, Salary Period, Apply URL, Job Description
  Company     K-Q: Company Name, Company Website, Company Size, Revenue,
                   CEO Name, Company Description, Benefits
  Location    R-S: City, State
  Outreach    T-AA: DM Name, DM Title, LinkedIn URL, Email,
                    First Name, Last Name, Email Body, Added to Instantly
"""

import os
import json
import time
import requests
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

SHEET_BATCH_SIZE = 10
SHEET_TITLE = "HR Leads - Apify Import v2"
TAB_NAME = "Leads"

DATASETS = [
    {"id": "hN5xfgLEy8g0VtbLH", "name": "UTAH - HR"},
    {"id": "99feNqRsugrlFHK9e", "name": "Idaho - HR"},
    {"id": "WBkUKdrGTYkbbmpVq", "name": "NEVADA - HR"},
    {"id": "WAMYv38TzPKh0uohv", "name": "SC-HR"},
    {"id": "SCOgzZZBkbUTPy5uA", "name": "NC-HR"},
    {"id": "0NgVHopyBhf1dbh7i", "name": "TEXAS HR"},
    {"id": "LdsoidkoNfHqclK1w", "name": "Unknown - HR"},
]

HR_OPS_KEYWORDS = [
    "human resources", "hr ", " hr", "hris", "hrbp",
    "people operations", "people ops", "talent", "recruiting", "recruiter",
    "talent acquisition", "operations manager", "ops manager",
    " ops ", "operations director", "operations lead",
]

HEADERS = [
    # ── Job Info (A–J) ──────────────────────────────────────────────────
    "Job_Id",           # A  Indeed job key
    "Job Title",        # B  Position title
    "Job Type",         # C  Full-time / Part-time / Contract
    "Occupations",      # D  Indeed occupation categories
    "Date Published",   # E  Date job was posted (YYYY-MM-DD)
    "Salary Min",       # F  Numeric minimum salary
    "Salary Max",       # G  Numeric maximum salary
    "Salary Period",    # H  HOUR / YEAR
    "Apply URL",        # I  Direct employer application URL
    "Job Description",  # J  Full job description text
    # ── Company (K–Q) ───────────────────────────────────────────────────
    "Company Name",     # K
    "Company Website",  # L  Corporate website URL
    "Company Size",     # M  e.g. "201 to 500", "10,000+"
    "Revenue",          # N  e.g. "more than $10B (USD)"
    "CEO Name",         # O
    "Company Description",  # P  Brief company description
    "Benefits",         # Q  Benefits offered, comma-separated
    # ── Location (R–S) ──────────────────────────────────────────────────
    "City",             # R
    "State",            # S  2-letter state code
    # ── Outreach (T–AA) — filled by downstream phases ───────────────────
    "DM Name",          # T  Decision maker full name
    "DM Title",         # U  Decision maker job title
    "LinkedIn URL",     # V  DM LinkedIn profile
    "Email",            # W  DM email address
    "First Name",       # X  Casualized first name
    "Last Name",        # Y
    "Email Body",       # Z  Generated outreach email
    "Added to Instantly",  # AA
]


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


def create_sheet(service):
    resp = service.spreadsheets().create(
        body={"properties": {"title": SHEET_TITLE}},
        fields="spreadsheetId",
    ).execute()
    sheet_id = resp["spreadsheetId"]
    print(f"  Created: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    return sheet_id


def setup_tab(service, sheet_id):
    """Rename default tab and write headers."""
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


def fetch_dataset(dataset_id, dataset_name):
    """Fetch all items from an Apify dataset with pagination."""
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
            print(f"  ERROR {dataset_name} (offset={offset}): HTTP {resp.status_code}")
            break

        items = resp.json()
        if not items:
            break

        all_items.extend(items)
        print(f"  {dataset_name}: {len(all_items)} fetched...", end="\r")

        if len(items) < APIFY_PAGE_SIZE:
            break
        offset += APIFY_PAGE_SIZE

    print(f"  {dataset_name}: {len(all_items)} total items fetched    ")
    return all_items


def is_hr_ops(title):
    t = (title or "").lower()
    return any(kw in t for kw in HR_OPS_KEYWORDS)


def fmt_salary(val):
    """Return salary as integer string, or empty."""
    if val is None:
        return ""
    try:
        return str(int(round(float(val))))
    except (ValueError, TypeError):
        return str(val)


def map_to_row(item):
    """Map an Apify Indeed item to the 27-column sheet row."""
    emp = item.get("employer") or {}
    loc = item.get("location") or {}
    sal = item.get("baseSalary") or {}
    desc_obj = item.get("description") or {}
    job_types = item.get("jobTypes") or {}
    occupations = item.get("occupations") or {}
    benefits = item.get("benefits") or {}

    # Job type — values of the jobTypes dict
    job_type = ", ".join(v for v in job_types.values() if v)

    # Occupations — values joined
    occ_str = ", ".join(v for v in occupations.values() if v)

    # Date — strip time portion
    date_raw = item.get("datePublished") or ""
    date_str = date_raw[:10] if date_raw else ""

    # Salary
    sal_min = fmt_salary(sal.get("min"))
    sal_max = fmt_salary(sal.get("max"))
    sal_unit = (sal.get("unitOfWork") or "").upper()

    # Description text — cap at 3000 chars
    description = (desc_obj.get("text") or "")[:3000]

    # Benefits — first 8 values
    benefits_str = ", ".join(list(benefits.values())[:8])

    return [
        # Job Info (A–J)
        item.get("key", ""),               # A: Job_Id
        item.get("title", ""),             # B: Job Title
        job_type,                          # C: Job Type
        occ_str,                           # D: Occupations
        date_str,                          # E: Date Published
        sal_min,                           # F: Salary Min
        sal_max,                           # G: Salary Max
        sal_unit,                          # H: Salary Period
        item.get("jobUrl", ""),            # I: Apply URL
        description,                       # J: Job Description
        # Company (K–Q)
        emp.get("name", ""),               # K: Company Name
        emp.get("corporateWebsite", ""),   # L: Company Website
        emp.get("employeesCount", ""),     # M: Company Size
        emp.get("revenue", ""),            # N: Revenue
        emp.get("ceoName", ""),            # O: CEO Name
        emp.get("briefDescription", ""),   # P: Company Description
        benefits_str,                      # Q: Benefits
        # Location (R–S)
        loc.get("city", ""),               # R: City
        loc.get("admin1Code", ""),         # S: State
        # Outreach (T–AA) — blank, filled by downstream phases
        "", "", "", "", "", "", "", "",    # T–AA
    ]


def write_rows(service, sheet_id, rows):
    total = len(rows)
    for i in range(0, total, SHEET_BATCH_SIZE):
        batch = rows[i:i + SHEET_BATCH_SIZE]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{TAB_NAME}'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": batch},
        ).execute()
        print(f"  Written {min(i + SHEET_BATCH_SIZE, total)}/{total} rows...", end="\r")
        time.sleep(1.5)
    print(f"  Written {total} rows total.          ")


def main():
    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Fetch Apify Datasets → Google Sheet ===\n")

    service = get_google_service()

    print("[1/3] Creating Google Sheet...")
    sheet_id = create_sheet(service)
    setup_tab(service, sheet_id)

    print("\n[2/3] Fetching and filtering datasets...")
    all_rows = []
    total_fetched = 0

    for ds in DATASETS:
        print(f"\n  ▸ {ds['name']} ({ds['id']})")
        items = fetch_dataset(ds["id"], ds["name"])
        total_fetched += len(items)

        filtered = [item for item in items if is_hr_ops(item.get("title", ""))]
        print(f"  → {len(filtered)} HR/Ops matches (of {len(items)})")

        for item in filtered:
            all_rows.append(map_to_row(item))

    print(f"\n  Total: {total_fetched} scraped → {len(all_rows)} HR/Ops leads")

    print(f"\n[3/3] Writing {len(all_rows)} rows...")
    if all_rows:
        write_rows(service, sheet_id, all_rows)
    else:
        print("  No rows to write.")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(f"\n=== Done ===")
    print(f"Sheet:  {sheet_url}")
    print(f"Rows:   {len(all_rows)}")


if __name__ == "__main__":
    main()
