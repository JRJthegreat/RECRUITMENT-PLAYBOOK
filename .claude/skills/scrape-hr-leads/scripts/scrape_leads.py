"""
Phase 1: Scrape HR job openings from TheirStack → Google Sheets

Calls TheirStack API, deduplicates against existing sheet data,
and appends new jobs. No DM logic here — that's the agent's job.
"""

import os
import sys
import json
import argparse
import time
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load .env from the skill's parent .claude directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

# Constants
THEIRSTACK_URL = "https://api.theirstack.com/v1/jobs/search"
PAGE_SIZE = 10
DEFAULT_DAYS = 15
MAX_EMPLOYEES = 5000
MIN_EMPLOYEES = 10
RETRY_LIMIT = 3
RETRY_DELAY = 2

# Keywords that indicate a company IS a staffing/recruitment firm (skip these)
STAFFING_KEYWORDS = [
    "staffing", "recruiting firm", "recruitment firm", "recruitment agency",
    "staffing agency", "staffing solutions", "workforce solutions",
    "talent solutions", "placing specialists", "placing candidates",
    "executive search", "headhunting", "headhunter",
    "contract staffing", "temp agency", "temporary staffing",
    "direct hire and contract", "full-service recruiting",
    "consultative staffing", "IT staffing",
]

# Industries that are almost always staffing firms
STAFFING_INDUSTRIES = [
    "staffing and recruiting",
    "human resources services",
]


def is_staffing_firm(company_description, company_industry):
    """Return True if the company appears to be a staffing/recruitment firm."""
    desc_lower = (company_description or "").lower()
    industry_lower = (company_industry or "").lower()

    # Check industry first (fast)
    for ind in STAFFING_INDUSTRIES:
        if ind in industry_lower:
            return True

    # Check description keywords
    for kw in STAFFING_KEYWORDS:
        if kw in desc_lower:
            return True

    return False


# Seniority ranking for company dedup — higher = more valuable
SENIORITY_RANK = {
    "chief": 10, "vp": 9, "vice president": 9, "head of": 8,
    "director": 7, "senior manager": 6, "manager": 5, "senior": 4,
    "lead": 3, "specialist": 2, "generalist": 2, "analyst": 2,
    "coordinator": 1, "assistant": 1, "recruiter": 2, "sr recruiter": 3,
}


def job_seniority_score(job_title):
    """Score a job title by seniority for company dedup. Higher = more valuable."""
    title_lower = (job_title or "").lower()
    best = 0
    for keyword, score in SENIORITY_RANK.items():
        if keyword in title_lower:
            best = max(best, score)
    return best


def dedup_by_company(jobs):
    """Keep only the highest-value job per company."""
    best_per_company = {}  # company_name_lower → (score, job)
    for job in jobs:
        company = (job.get("company") or "").strip().lower()
        if not company:
            continue
        score = job_seniority_score(job.get("job_title", ""))
        existing = best_per_company.get(company)
        if not existing or score > existing[0]:
            best_per_company[company] = (score, job)

    kept = [item[1] for item in best_per_company.values()]
    removed = len(jobs) - len(kept)
    return kept, removed


# Full list of HR job titles to search on TheirStack
# This IS the filter — TheirStack only returns jobs matching these titles.
HR_JOB_TITLES = [
    # Core HR
    "HR Generalist", "HR Manager", "HR Director",
    "HR Coordinator", "HR Administrator", "HR Assistant",
    "Human Resource Generalist", "Human Resources Manager",
    "HR Business Partner", "HRBP",
    # People / Leadership
    "Head of People", "Head of HR", "Head of Human Resources",
    "VP of People", "VP of HR", "VP Human Resources",
    "Chief People Officer", "Chief Human Resources Officer", "CHRO",
    "People Operations Manager", "People Partner",
    "Director of People", "Director of HR",
    # Talent / Recruiting
    "Talent Acquisition", "Talent Acquisition Manager",
    "Talent Acquisition Specialist", "Recruiting Manager",
    "Recruiter", "Recruiting Operations Manager",
    "Talent Acquisition Systems Administrator",
    # HRIS / Systems
    "HRIS Analyst", "HRIS Administrator", "HRIS Manager", "HRIS",
    "HR Systems Manager", "HR Systems Director",
    "HR Systems Architect", "HRIS Implementation Consultant",
    "HR Technology Project Manager",
    # Compensation / Benefits / Payroll
    "Compensation Analyst", "Total Rewards Manager",
    "Benefits Administrator", "Benefits Analyst",
    "Payroll Manager", "Payroll Analyst",
    # Analytics / Workforce
    "People Analytics Specialist", "Workforce Planning Analyst",
    "HR Data Scientist", "Employee Experience Analyst",
    # Compliance / Specialized
    "HR Compliance Specialist", "Immigration Specialist",
]

# Sheet column headers — matches Jude's sheet structure
HEADERS = [
    # ID + DM info (cols A-D)
    "Job_Id", "person_name", "result_title", "linkedin_url",
    # Phase 2 fills (col E)
    "email",
    # TheirStack data (cols F-W)
    "company name", "job_title", "url", "posted_date",
    "job_country_code", "is_remote", "employment_status", "seniority",
    "job_location", "job_description", "salary",
    "company_url", "company_linkedin_url", "company_industry",
    "company_employee_count", "company_revenue_usd", "company_description",
    "company_city",
    # Agent extras (cols X-Y)
    "dm_confidence", "dm_reasoning",
    # Phase 3 fills (cols Z-AC)
    "First name", "Last name", "Body", "Added to instantly",
]


def get_sheet_id_from_url(url):
    """Extract spreadsheet ID from a Google Sheets URL."""
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url  # Assume raw ID was passed


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
        # Save refreshed token
        token_data["token"] = creds.token
        with open(token_path, "w") as f:
            json.dump(token_data, f)

    return build("sheets", "v4", credentials=creds)


def ensure_headers(service, sheet_id):
    """Ensure the sheet has the correct headers. Creates them if missing."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Sheet1!1:1"
    ).execute()
    existing = result.get("values", [[]])[0]

    if not existing:
        # Empty sheet — write all headers
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        print(f"  Created {len(HEADERS)} column headers")
    else:
        # Check if headers match
        if existing != HEADERS[:len(existing)]:
            print(f"  Warning: existing headers don't match expected. Using existing sheet structure.")


def get_existing_job_ids(service, sheet_id):
    """Read the Job_Id column to build a dedup set."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Sheet1!A:A"  # Column A = Job_Id
    ).execute()
    rows = result.get("values", [])
    # Skip header row, collect non-empty values
    ids = set()
    for row in rows[1:]:
        if row and row[0].strip():
            ids.add(row[0].strip())
    return ids


def theirstack_search(api_key, page, days, min_employees=MIN_EMPLOYEES, max_employees=MAX_EMPLOYEES):
    """Search TheirStack for HR job openings. Returns (jobs_list, total_results)."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "include_total_results": True,
        "order_by": [{"desc": False, "field": "date_posted"}],
        "posted_at_max_age_days": days,
        "job_country_code_or": ["US"],
        "job_title_or": HR_JOB_TITLES,
        "job_title_not": ["Temporary", "Assistant", "Part-time", "Intern"],
        "industry_id_not": [104, 10, 100],
        "max_employee_count": max_employees,
        "min_employee_count_or_null": min_employees,
        "company_type": "direct_employer",
        "employment_statuses_or": ["full_time"],
        "page": page,
        "limit": PAGE_SIZE,
        "blur_company_data": False,
    }

    for attempt in range(RETRY_LIMIT):
        try:
            resp = requests.post(THEIRSTACK_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                total = data.get("total_results", 0)
                return data.get("data", []), total
            elif resp.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  *** TheirStack API error {resp.status_code}: {resp.text[:200]}")
                print(f"  *** SCRAPE INCOMPLETE — stopped at page {page}")
                return None, 0  # None signals error (vs empty [] for no results)
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}")
            if attempt < RETRY_LIMIT - 1:
                time.sleep(RETRY_DELAY)
    return [], 0


def parse_employee_count(count_range):
    """Parse employee count range string to an integer (use upper bound)."""
    if not count_range:
        return None
    s = str(count_range).strip()
    # Handle formats like "50-200", "1001-5000", "10001+"
    s = s.replace(",", "").replace("+", "")
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])  # upper bound
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def job_to_row(job):
    """Convert a TheirStack job dict to a sheet row matching reference structure."""
    company = job.get("company_object") or {}
    emp_count = company.get("employee_count_range") or company.get("employee_count") or ""

    return [
        # ID + DM info (A-D) — DM columns blank on scrape
        str(job.get("id", "")),                          # Job_Id
        "", "", "",                                      # person_name, result_title, linkedin_url
        # Phase 2 (E) — blank on scrape
        "",                                              # email
        # TheirStack data (F-W)
        job.get("company", ""),                          # company name
        job.get("job_title", ""),                        # job_title
        job.get("source_url", ""),                       # url
        job.get("date_posted", ""),                      # posted_date
        job.get("country_code", ""),                     # job_country_code
        str(job.get("remote", "")),                      # is_remote
        (job.get("employment_statuses") or [""])[0],     # employment_status
        job.get("seniority", ""),                        # seniority
        job.get("location", ""),                         # job_location
        (job.get("description", "") or "")[:2000],       # job_description
        job.get("salary_string", ""),                    # salary
        company.get("domain") or "",                     # company_url
        company.get("linkedin_url", ""),                 # company_linkedin_url
        company.get("industry", ""),                     # company_industry
        str(emp_count),                                  # company_employee_count
        company.get("annual_revenue_usd_readable", ""),  # company_revenue_usd
        (company.get("long_description", "") or "")[:1000],  # company_description
        company.get("city", ""),                         # company_city
        # Agent extras (X-Y) — blank on scrape
        "", "",
        # Phase 3 fills (Z-AC) — blank on scrape
        "", "", "", "",
    ]


def append_rows(service, sheet_id, rows, token_path=None):
    """Append rows to the sheet in small batches with retry and token refresh."""
    if not rows:
        return
    BATCH_SIZE = 10
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        for attempt in range(3):
            try:
                service.spreadsheets().values().append(
                    spreadsheetId=sheet_id,
                    range="Sheet1!A:A",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": batch},
                ).execute()
                break
            except Exception as e:
                if attempt < 2:
                    print(f"  Batch {i//BATCH_SIZE + 1} failed, retrying... ({e})")
                    time.sleep(2)
                    # Rebuild service with fresh token
                    if token_path:
                        try:
                            service = get_google_service(token_path)
                        except Exception:
                            pass
                else:
                    raise
        done = min(i + BATCH_SIZE, len(rows))
        if done % 100 == 0 or done == len(rows):
            print(f"  Appended {done}/{len(rows)} rows...")


def main():
    parser = argparse.ArgumentParser(description="Scrape HR jobs from TheirStack into Google Sheets")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"Max age of job postings in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--start_page", type=int, default=0, help="Page number to start from (for resuming interrupted scrapes)")
    args = parser.parse_args()

    # Validate env
    api_key = os.getenv("THEIRSTACK_API_KEY")
    if not api_key:
        print("Error: THEIRSTACK_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)
    ensure_headers(service, sheet_id)

    # Get existing job IDs for dedup
    print("Loading existing job IDs for deduplication...")
    existing_ids = get_existing_job_ids(service, sheet_id)
    print(f"  Found {len(existing_ids)} existing jobs in sheet")

    # Scrape TheirStack — page by page, filter + write each batch immediately
    start_msg = f"\nScraping TheirStack (posted in last {args.days} days, {MIN_EMPLOYEES}-{MAX_EMPLOYEES} employees, oldest first)"
    if args.start_page > 0:
        start_msg += f", resuming from page {args.start_page}"
    print(start_msg + "...")
    total_new = 0
    total_written = 0
    page = args.start_page
    total_results = None
    skipped_dup = 0
    skipped_staffing = 0
    skipped_company_dup = 0
    # Track companies seen this run for cross-page dedup
    companies_seen = {}  # company_lower → (seniority_score, job_dict)

    while True:
        jobs, total = theirstack_search(api_key, page, args.days)
        if total_results is None and total:
            total_results = total
            print(f"  Total results from TheirStack: {total_results}")

        if jobs is None:
            print(f"\n  *** SCRAPE STOPPED DUE TO API ERROR at page {page} ***")
            print(f"  *** Data written so far is safe. Re-run to continue from where it left off.")
            break
        if not jobs:
            print(f"  Page {page}: no more results, done.")
            break

        # Filter this page's jobs
        page_jobs = []
        for job in jobs:
            job_id = str(job.get("id", ""))
            if job_id in existing_ids:
                skipped_dup += 1
                continue

            # Filter out staffing/recruitment firms
            company_obj = job.get("company_object") or {}
            desc = company_obj.get("long_description", "") or ""
            industry = company_obj.get("industry", "") or ""
            if is_staffing_firm(desc, industry):
                skipped_staffing += 1
                existing_ids.add(job_id)
                continue

            # Company dedup: keep highest-value role per company
            company_name = (job.get("company") or "").strip().lower()
            score = job_seniority_score(job.get("job_title", ""))
            if company_name in companies_seen:
                prev_score, prev_job = companies_seen[company_name]
                if score > prev_score:
                    # New job is better — replace (old one already written, but that's ok)
                    companies_seen[company_name] = (score, job)
                    page_jobs.append(job)
                    existing_ids.add(job_id)
                else:
                    skipped_company_dup += 1
                    existing_ids.add(job_id)
                    continue
            else:
                companies_seen[company_name] = (score, job)
                page_jobs.append(job)
                existing_ids.add(job_id)

        # Write this page's filtered jobs to sheet immediately
        if page_jobs:
            rows = [job_to_row(job) for job in page_jobs]
            for attempt in range(3):
                try:
                    service.spreadsheets().values().append(
                        spreadsheetId=sheet_id,
                        range="Sheet1!A:A",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": rows},
                    ).execute()
                    break
                except Exception as e:
                    if attempt < 2:
                        print(f"    Sheet write retry {attempt + 1}... ({e})")
                        time.sleep(2)
                        service = get_google_service(token_path)
                    else:
                        print(f"    Sheet write FAILED for page {page}: {e}")
            total_written += len(page_jobs)

        total_new += len(page_jobs)
        page += 1
        print(f"  Page {page}: +{len(page_jobs)} written | total: {total_new} new, {skipped_dup} dup, {skipped_staffing} staffing, {skipped_company_dup} company dup")

        # Stop if exhausted all pages
        if total_results and page * PAGE_SIZE >= total_results:
            break

        # Small delay to be nice to the API
        time.sleep(0.5)

    # Sort sheet by posted_date (oldest first)
    print("\nSorting sheet by posted_date (oldest first)...")
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet_gid = meta["sheets"][0]["properties"]["sheetId"]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [{
                "sortRange": {
                    "range": {"sheetId": sheet_gid, "startRowIndex": 1},
                    "sortSpecs": [{"dimensionIndex": 8, "sortOrder": "ASCENDING"}],
                }
            }]
        },
    ).execute()
    print("  Done.")

    # Summary
    print(f"\n{'='*50}")
    print(f"Phase 1 Complete")
    print(f"  New jobs written to sheet: {total_written}")
    print(f"  Duplicates skipped: {skipped_dup}")
    print(f"  Staffing firms filtered: {skipped_staffing}")
    print(f"  Same-company duplicates: {skipped_company_dup}")
    print(f"  Pages scanned: {page}")
    print(f"  Sheet: {args.sheet_url}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
