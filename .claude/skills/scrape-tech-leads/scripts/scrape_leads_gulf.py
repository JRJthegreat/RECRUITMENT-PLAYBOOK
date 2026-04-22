"""
Phase 1: Scrape tech job openings from TheirStack → Google Sheets (Gulf region)

Calls TheirStack API for UAE + Saudi Arabia tech roles, filters out contract/temp positions,
deduplicates against existing sheet data, and appends to the Data tab.
"""

import os
import sys
import json
import argparse
import math
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
API_PAGE_SIZE = 500       # Max results per TheirStack API call (paid plan limit)
SHEET_BATCH_SIZE = 10     # Write to Google Sheets in small batches to avoid failures
DEFAULT_DAYS = 7
RETRY_LIMIT = 3
RETRY_DELAY = 2

# Gulf region countries
GULF_LOCATIONS = [
    {"id": 290557},   # United Arab Emirates
    {"id": 102358},   # Saudi Arabia
]

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

    for ind in STAFFING_INDUSTRIES:
        if ind in industry_lower:
            return True

    for kw in STAFFING_KEYWORDS:
        if kw in desc_lower:
            return True

    return False


# Seniority ranking for company dedup — higher = more valuable (tech-weighted)
SENIORITY_RANK = {
    "chief": 10, "cto": 10, "vp": 9, "vice president": 9, "head of": 8,
    "director": 7, "principal": 6, "staff": 5, "senior": 4,
    "lead": 3, "architect": 3, "scientist": 3,
    "engineer": 2, "developer": 2, "manager": 2,
}


def job_seniority_score(job_title):
    """Score a job title by seniority for company dedup. Higher = more valuable."""
    title_lower = (job_title or "").lower()
    best = 0
    for keyword, score in SENIORITY_RANK.items():
        if keyword in title_lower:
            best = max(best, score)
    return best


# Tech job titles to search on TheirStack — client's niche roles only
TECH_JOB_TITLES = [
    # NLP / NLU
    "NLP Engineer", "Natural Language Processing Engineer", "NLP Scientist",
    # Solutions Architecture
    "Solutions Architect", "Cloud Solutions Architect", "Enterprise Architect",
    # Data Engineering (senior+)
    "Lead Data Engineer", "Senior Data Engineer", "Staff Data Engineer", "Principal Data Engineer",
    # AI / ML
    "AI Engineer", "Machine Learning Engineer", "ML Engineer", "Applied AI Engineer", "AI/ML Engineer",
    # Web3 / Blockchain
    "Web3 Engineer", "Blockchain Engineer", "Solidity Developer", "Smart Contract Engineer", "Web3 Developer",
    # GenAI / LLM
    "LLM Engineer", "GenAI Engineer", "Generative AI Engineer",
    # MLOps / ML Platform
    "MLOps Engineer", "ML Platform Engineer",
    # Computer Vision
    "Computer Vision Engineer",
    # Data Science (senior)
    "Data Scientist",
    # Platform / DevOps / SRE
    "Platform Engineer", "DevOps Engineer", "SRE",
    # Niche languages
    "Rust Engineer", "Go Engineer",
    # Engineering leadership
    "Engineering Manager", "VP Engineering",
    # From client's TheirStack query
    "CTO", "Software Engineer", "tech lead", "Machine learning", "Deep learning",
]

# Extended keywords for job_description_contains_or (superset of titles + extras)
TECH_DESCRIPTION_KEYWORDS = TECH_JOB_TITLES + [
    "Fintech", "Azure Ops", "Chief Technology Officer",
    "head of engineerig", "VP of engineering", "Quantum Computing",
    # Catch roles by tech stack / domain in description
    "LLM", "large language model", "GenAI", "generative AI",
    "PyTorch", "TensorFlow", "computer vision",
]

# Keywords for contract classification (title + description scan)
CONTRACT_KEYWORDS = [
    "contract", "contractor", "freelance", "freelancer",
    "fixed-term", "fixed term", "temporary", "temp position",
    "6-month", "6 month", "12-month", "12 month", "3-month", "3 month",
    "consulting", "consultant", "interim",
    "c2c", "corp-to-corp", "1099", "w2 contract",
    "statement of work", "sow",
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
        token_data["token"] = creds.token
        with open(token_path, "w") as f:
            json.dump(token_data, f)

    return build("sheets", "v4", credentials=creds)


def ensure_tab_exists(service, sheet_id, tab_name):
    """Ensure a tab exists in the spreadsheet. Creates it if missing. Returns sheet GID."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sheet in meta.get("sheets", []):
        props = sheet["properties"]
        if props["title"] == tab_name:
            return props["sheetId"]

    # Create the tab
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    new_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    print(f"  Created tab '{tab_name}'")
    return new_gid


def ensure_headers(service, sheet_id, tab_name):
    """Ensure the tab has the correct headers. Creates them if missing."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!1:1"
    ).execute()
    existing = result.get("values", [[]])[0]

    if not existing:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        print(f"  Created {len(HEADERS)} column headers in '{tab_name}'")
    else:
        if existing != HEADERS[:len(existing)]:
            print(f"  Warning: existing headers in '{tab_name}' don't match expected. Using existing sheet structure.")


def get_existing_job_ids(service, sheet_id, tab_name):
    """Read the Job_Id column to build a dedup set."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:A"
    ).execute()
    rows = result.get("values", [])
    ids = set()
    for row in rows[1:]:
        if row and row[0].strip():
            ids.add(row[0].strip())
    return ids


def classify_employment_type(job):
    """Classify a job as 'perm' or 'contract' based on TheirStack data + heuristics."""
    # Step 1: Check TheirStack employment_statuses field
    statuses = job.get("employment_statuses") or []
    statuses_lower = [s.lower() for s in statuses]

    for s in statuses_lower:
        if s in ("contractor", "part_time", "temporary"):
            return "contract"
    for s in statuses_lower:
        if s == "full_time":
            return "perm"

    # Step 2: Scan title + description for contract indicators
    title = (job.get("job_title") or "").lower()
    desc = (job.get("description") or "")[:2000].lower()
    text = f"{title} {desc}"

    for kw in CONTRACT_KEYWORDS:
        if kw in text:
            return "contract"

    # Default to perm
    return "perm"


def theirstack_search(api_key, page, days):
    """Search TheirStack for tech job openings in UAE + Saudi Arabia. Returns (jobs_list, total_results)."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "include_total_results": True,
        "posted_at_max_age_days": days,
        "job_location_or": GULF_LOCATIONS,
        "job_title_pattern_or": TECH_JOB_TITLES,
        "job_title_not": ["Intern", "Graduate", "junior"],
        "company_type": "direct_employer",
        "job_description_contains_or": TECH_DESCRIPTION_KEYWORDS,
        "min_employee_count_or_null": 1,
        "max_employee_count_or_null": 500,
        "industry_id_not": [104],
        "page": page,
        "limit": API_PAGE_SIZE,
        "blur_company_data": False,
    }

    for attempt in range(RETRY_LIMIT):
        try:
            resp = requests.post(THEIRSTACK_URL, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                metadata = data.get("metadata") or {}
                total = metadata.get("total_results", 0)
                return data.get("data", []), total
            elif resp.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            elif resp.status_code == 504:
                wait = 10 * (2 ** attempt)
                print(f"  Server timeout (504), retrying in {wait}s (attempt {attempt + 1}/{RETRY_LIMIT})...")
                time.sleep(wait)
            elif resp.status_code >= 500:
                wait = 5 * (2 ** attempt)
                print(f"  Server error {resp.status_code}, retrying in {wait}s (attempt {attempt + 1}/{RETRY_LIMIT})...")
                time.sleep(wait)
            else:
                print(f"  *** TheirStack API error {resp.status_code}: {resp.text[:200]}")
                print(f"  *** SCRAPE INCOMPLETE — stopped at page {page}")
                return None, 0
        except requests.exceptions.Timeout:
            wait = 10 * (2 ** attempt)
            print(f"  Request timeout, retrying in {wait}s (attempt {attempt + 1}/{RETRY_LIMIT})...")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = 5 * (2 ** attempt)
            print(f"  Request error: {e}, retrying in {wait}s (attempt {attempt + 1}/{RETRY_LIMIT})...")
            time.sleep(wait)

    print(f"  *** All {RETRY_LIMIT} retries failed at page {page}")
    print(f"  *** Re-run with --start_page {page} to resume")
    return None, 0


def count_check(api_key, days):
    """Call TheirStack with limit=1 to get the total lead count before scraping."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "include_total_results": True,
        "posted_at_max_age_days": days,
        "job_location_or": GULF_LOCATIONS,
        "job_title_pattern_or": TECH_JOB_TITLES,
        "job_title_not": ["Intern", "Graduate", "junior"],
        "company_type": "direct_employer",
        "job_description_contains_or": TECH_DESCRIPTION_KEYWORDS,
        "min_employee_count_or_null": 1,
        "max_employee_count_or_null": 500,
        "industry_id_not": [104],
        "page": 0,
        "limit": 1,
        "blur_company_data": False,
    }
    resp = requests.post(THEIRSTACK_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return (resp.json().get("metadata") or {}).get("total_results", 0)


def parse_employee_count(count_range):
    """Parse employee count range string to an integer (use upper bound)."""
    if not count_range:
        return None
    s = str(count_range).strip()
    s = s.replace(",", "").replace("+", "")
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
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


def append_rows_to_tab(service, sheet_id, tab_name, rows, token_path=None):
    """Append rows to a specific tab with retry and token refresh."""
    if not rows:
        return
    for attempt in range(3):
        try:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"'{tab_name}'!A:A",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
            return
        except Exception as e:
            is_rate_limit = "429" in str(e) or "RATE_LIMIT" in str(e)
            wait = 30 if is_rate_limit else 2
            if attempt < 2:
                print(f"    Sheet write retry {attempt + 1} (waiting {wait}s)... ({e})")
                time.sleep(wait)
                if token_path:
                    try:
                        service = get_google_service(token_path)
                    except Exception:
                        pass
            else:
                print(f"    Sheet write FAILED for tab '{tab_name}': {e}")


def sort_tab(service, sheet_id, tab_gid):
    """Sort a tab by posted_date column (col I = index 8), oldest first."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [{
                "sortRange": {
                    "range": {"sheetId": tab_gid, "startRowIndex": 1},
                    "sortSpecs": [{"dimensionIndex": 8, "sortOrder": "ASCENDING"}],
                }
            }]
        },
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="Scrape tech jobs from TheirStack (UAE + Saudi Arabia) into Google Sheets")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"Max age of job postings in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--limit", type=int, default=0, help="Max total jobs to scrape (0 = all)")
    parser.add_argument("--start_page", type=int, default=0, help="Page number to start from (for resuming)")
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

    # Pre-flight: show total count and ask for confirmation
    print(f"\nChecking TheirStack for available leads in UAE + Saudi Arabia (last {args.days} days)...")
    total_available = count_check(api_key, args.days)
    pages_needed = math.ceil(total_available / API_PAGE_SIZE) if total_available else 0
    print(f"  Found {total_available:,} total leads across {pages_needed} page(s) of {API_PAGE_SIZE}.")
    if args.limit:
        print(f"  You set --limit {args.limit}, so at most {args.limit} will be written.")
    confirm = input("\nProceed with scrape? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Ensure Data tab exists with headers
    data_gid = ensure_tab_exists(service, sheet_id, "Data")
    ensure_headers(service, sheet_id, "Data")

    # Get existing job IDs for dedup
    print("Loading existing job IDs for deduplication...")
    existing_ids = get_existing_job_ids(service, sheet_id, "Data")
    print(f"  Found {len(existing_ids)} existing jobs")

    # Scrape TheirStack — page by page, classify + write each batch immediately
    start_msg = f"\nScraping TheirStack (UAE + Saudi Arabia, 10-500 employees, posted in last {args.days} days)"
    if args.start_page > 0:
        start_msg += f", resuming from page {args.start_page}"
    if args.limit:
        start_msg += f", limit {args.limit} jobs"
    print(start_msg + "...")

    total_new = 0
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
        rows = []
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
                    companies_seen[company_name] = (score, job)
                else:
                    skipped_company_dup += 1
                    existing_ids.add(job_id)
                    continue
            else:
                companies_seen[company_name] = (score, job)

            existing_ids.add(job_id)
            rows.append(job_to_row(job))

        # Write to sheet in batches of SHEET_BATCH_SIZE (with 1.5s delay to stay under 60 writes/min)
        for i in range(0, len(rows), SHEET_BATCH_SIZE):
            batch = rows[i:i + SHEET_BATCH_SIZE]
            append_rows_to_tab(service, sheet_id, "Data", batch, token_path)
            if i + SHEET_BATCH_SIZE < len(rows):
                time.sleep(1.5)

        total_new += len(rows)
        page += 1
        print(f"  API page {page}: fetched {len(jobs)}, +{len(rows)} added | total: {total_new} new, {skipped_dup} dup, {skipped_staffing} staffing, {skipped_company_dup} company dup")

        # Check limit
        if args.limit and total_new >= args.limit:
            print(f"  Reached limit of {args.limit} jobs.")
            break

        # Stop if exhausted all pages
        if total_results and page * API_PAGE_SIZE >= total_results:
            break

        # Small delay to be nice to the API
        time.sleep(0.5)

    # Sort by posted_date (oldest first)
    print("\nSorting by posted_date (oldest first)...")
    sort_tab(service, sheet_id, data_gid)
    print("  Done.")

    # Summary
    print(f"\n{'='*50}")
    print(f"Phase 1 Complete (Gulf)")
    print(f"  Jobs written: {total_new}")
    print(f"  Duplicates skipped: {skipped_dup}")
    print(f"  Staffing firms filtered: {skipped_staffing}")
    print(f"  Same-company duplicates: {skipped_company_dup}")
    print(f"  Pages scanned: {page}")
    print(f"  Sheet: {args.sheet_url}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
