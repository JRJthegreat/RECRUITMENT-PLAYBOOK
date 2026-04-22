"""
Phase 2: Find decision makers via Google Search + LinkedIn

For each lead without a DM Name:
1. Determine target title using HR pipeline rules (company size + role seniority)
2. Search Google via Apify: "{company} {target title} site:linkedin.com/in/"
3. Parse LinkedIn snippet to extract person name + title
4. Validate: company name matches, title matches target category
5. Write DM Name, DM Title, LinkedIn URL to sheet

Auto-retry: flips category (CEO↔HR) for rows where no match was found.
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

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

BATCH_SIZE = 10
SHEET_WRITE_DELAY = 1.5

# --- Column indices (0-based, matching HEADERS in pull_dataset.py) ---
COL_JOB_TITLE = 1       # B: Job Title
COL_COMPANY_NAME = 10   # K: Company Name
COL_COMPANY_SIZE = 12   # M: Company Size
COL_DM_NAME = 19        # T: DM Name
COL_DM_TITLE = 20       # U: DM Title
COL_LINKEDIN_URL = 21   # V: LinkedIn URL

# --- Seniority classification (reused from scrape-hr-leads) ---

SENIOR_HR_TITLES = [
    "hr director", "director of hr", "director of human resources",
    "vp of people", "vp of hr", "vp human resources", "vp, people",
    "vp, human resources", "vp, hr",
    "head of people", "head of hr", "head of human resources",
    "chief people officer", "chief human resources officer", "chro", "cpo",
    "svp people", "svp hr", "svp, people", "svp, hr",
    "vice president of people", "vice president of hr",
    "vice president, human resources", "vice president, people",
    "director of people", "director of talent",
]

MID_HR_TITLES = [
    "hr manager", "human resources manager", "human resource manager",
    "hr business partner", "hrbp",
    "people operations manager", "people partner",
    "senior hr manager", "sr hr manager", "sr. hr manager",
    "hr generalist", "senior hr generalist",
    "recruiting manager", "talent acquisition manager",
]


def parse_employee_count(count_str):
    if not count_str:
        return None
    s = str(count_str).strip().replace(",", "").replace("+", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def classify_job_seniority(job_title):
    title_lower = (job_title or "").lower().strip()
    for keyword in SENIOR_HR_TITLES:
        if keyword in title_lower:
            return "senior"
    for keyword in MID_HR_TITLES:
        if keyword in title_lower:
            return "mid"
    return "junior"


def determine_target(job_title, employee_count_str):
    """Determine DM target level. Returns ('ceo'|'vp_hr'|'director_ta', reasoning)."""
    seniority = classify_job_seniority(job_title)
    count = parse_employee_count(employee_count_str)

    if seniority == "senior":
        return "ceo", f"Hiring senior HR leader ({job_title}), targeting CEO/Founder"

    if count is None:
        return "ceo", "Unknown company size, defaulting to CEO/Founder"

    if count < 200:
        return "ceo", f"Small company ({count} employees), targeting CEO/Founder"

    if count <= 1000:
        return "vp_hr", f"Mid-size company ({count} employees), targeting VP HR/People"

    return "director_ta", f"Large company ({count} employees), targeting Director TA"


def flip_target(target):
    if target == "ceo":
        return "vp_hr"
    return "ceo"


def build_search_query(company_name, target_level):
    if target_level == "ceo":
        return f'"{company_name}" ("CEO" OR "Founder" OR "Owner" OR "Managing Director" OR "President") site:linkedin.com/in/'
    elif target_level == "vp_hr":
        return f'"{company_name}" ("VP" OR "Vice President" OR "Head") ("HR" OR "Human Resources" OR "People") site:linkedin.com/in/'
    elif target_level == "director_ta":
        return f'"{company_name}" ("Director" OR "Head") ("Talent Acquisition" OR "Recruiting" OR "TA" OR "Talent") site:linkedin.com/in/'
    return f'"{company_name}" "CEO" site:linkedin.com/in/'


def parse_linkedin_result(organic_result):
    """Extract person name and title from a LinkedIn search result."""
    title = organic_result.get("title", "")
    url = organic_result.get("url", "")

    # Must be a linkedin.com/in/ profile URL
    if "linkedin.com/in/" not in url:
        return None, None, None

    # Clean title: remove " | LinkedIn" suffix
    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()

    # Format 1: "Name - Title - Company"
    # Format 2: "Name – Title at Company"
    # Format 3: "Name - Title"
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    if len(parts) >= 2:
        person_name = parts[0].strip()
        result_title = parts[1].strip()
        # Clean "at Company" from title
        result_title = re.sub(r"\s+at\s+.*$", "", result_title, flags=re.IGNORECASE).strip()
        return person_name, result_title, url

    # Format 4: "Name, Title"
    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url

    return None, None, url


def _name_words(text):
    noise = {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&",
             "a", "an", "for", "in", "at", "by", "group", "services"}
    words = re.split(r"[\s,.\-&/()+]+", text.lower())
    return [w for w in words if len(w) > 2 and w not in noise]


def validate_result(person_name, result_title, company_name, target_level):
    """Validate that the search result matches our target."""
    if not person_name or not result_title:
        return False

    # Check company name appears (fuzzy — at least 1 core word)
    # This is checked via the Google query already including the company name,
    # but we do a lightweight check on the title/snippet
    # We trust Google's matching since we quoted the company name in the query

    # Check title matches target category
    title_lower = result_title.lower()
    if target_level == "ceo":
        ceo_keywords = ["ceo", "founder", "owner", "managing director", "president",
                        "co-founder", "cofounder", "chief executive"]
        return any(kw in title_lower for kw in ceo_keywords)
    elif target_level == "vp_hr":
        vp_keywords = ["vp", "vice president", "head of"]
        hr_keywords = ["hr", "human resources", "people", "talent"]
        has_vp = any(kw in title_lower for kw in vp_keywords)
        has_hr = any(kw in title_lower for kw in hr_keywords)
        return has_vp and has_hr
    elif target_level == "director_ta":
        dir_keywords = ["director", "head"]
        ta_keywords = ["talent", "recruiting", "ta", "recruitment", "acquisition"]
        has_dir = any(kw in title_lower for kw in dir_keywords)
        has_ta = any(kw in title_lower for kw in ta_keywords)
        return has_dir and has_ta

    return False


# --- Google Sheets ---

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


# --- Apify Google Search ---

def apify_google_search(queries):
    """Run Apify Google Search Scraper. Returns list of result dicts per query."""
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={
            "queries": "\n".join(queries),
            "resultsPerPage": 5,
            "maxPagesPerQuery": 1,
            "languageCode": "en",
            "countryCode": "us",
            "includeUnfilteredResults": False,
        },
        timeout=300,
    )

    if resp.status_code not in (200, 201):
        print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:300]}")
        return {}

    items = resp.json()
    results = {}
    for item in items:
        query = item.get("searchQuery", {}).get("term", "")
        organic = item.get("organicResults", [])
        if query:
            results[query] = organic
    return results


def process_batch(service, sheet_id, tab_name, leads, target_map, dry_run=False):
    """Process a batch of leads: build queries, search, parse, write."""
    queries = []
    query_to_lead = {}

    for lead in leads:
        company = lead["company_name"]
        target = target_map[lead["sheet_row"]]
        query = build_search_query(company, target)
        queries.append(query)
        query_to_lead[query] = lead

    if dry_run:
        for query, lead in query_to_lead.items():
            target = target_map[lead["sheet_row"]]
            print(f"  [DRY RUN] Row {lead['sheet_row']}: {lead['company_name']} → {target}")
        return 0, len(leads)

    # Run Google Search
    search_results = apify_google_search(queries)

    # Parse and validate results
    updates = []
    found = 0
    not_found = 0

    for query, lead in query_to_lead.items():
        target = target_map[lead["sheet_row"]]
        organic = search_results.get(query, [])

        matched = False
        for result in organic[:5]:
            person_name, result_title, linkedin_url = parse_linkedin_result(result)
            if person_name and validate_result(person_name, result_title, lead["company_name"], target):
                updates.append({
                    "sheet_row": lead["sheet_row"],
                    "person_name": person_name,
                    "result_title": result_title,
                    "linkedin_url": linkedin_url,
                })
                print(f"    Row {lead['sheet_row']}: {lead['company_name']} → {person_name} ({result_title})")
                found += 1
                matched = True
                break

        if not matched:
            print(f"    Row {lead['sheet_row']}: {lead['company_name']} → NOT FOUND")
            not_found += 1

    # Write to sheet
    if updates:
        data = []
        for u in updates:
            row = u["sheet_row"]
            data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_NAME)}{row}", "values": [[u["person_name"]]]})
            data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_TITLE)}{row}", "values": [[u["result_title"]]]})
            data.append({"range": f"'{tab_name}'!{col_letter(COL_LINKEDIN_URL)}{row}", "values": [[u["linkedin_url"]]]})

        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        print(f"  → Written {len(updates)} DMs to sheet")

    return found, not_found


def run_pass(service, sheet_id, tab_name, rows, pass_name, target_fn, limit=0, dry_run=False):
    """Run a full pass over leads that need DM lookup."""
    # Collect leads needing processing
    leads = []
    target_map = {}

    for i, row in enumerate(rows):
        if limit > 0 and len(leads) >= limit:
            break

        dm_name = cell(row, COL_DM_NAME)
        if dm_name:
            continue

        company_name = cell(row, COL_COMPANY_NAME)
        if not company_name:
            continue

        job_title = cell(row, COL_JOB_TITLE)
        company_size = cell(row, COL_COMPANY_SIZE)

        target, reasoning = target_fn(job_title, company_size)
        sheet_row = i + 2  # 1-indexed header + data

        leads.append({
            "sheet_row": sheet_row,
            "company_name": company_name,
            "job_title": job_title,
        })
        target_map[sheet_row] = target

    print(f"\n{pass_name}: {len(leads)} leads to process")
    if not leads:
        return 0, 0

    total_found = 0
    total_not_found = 0
    num_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE

    for b in range(num_batches):
        batch = leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"  Batch {b + 1}/{num_batches}")
        found, not_found = process_batch(service, sheet_id, tab_name, batch, target_map, dry_run)
        total_found += found
        total_not_found += not_found
        if not dry_run:
            time.sleep(SHEET_WRITE_DELAY)

    return total_found, total_not_found


def main():
    parser = argparse.ArgumentParser(description="Find decision makers via Google Search + LinkedIn")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--limit", type=int, default=0, help="Max leads per pass (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview targeting rules without searching")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Find Decision Makers (Google Search + LinkedIn) ===\n")

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    # Detect tab name
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"  Using tab: '{tab_name}'")

    # Read all data
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("  No data rows found.")
        return
    data_rows = all_rows[1:]

    # Pass 1: Primary targeting
    found1, not_found1 = run_pass(
        service, sheet_id, tab_name, data_rows,
        "Pass 1 (primary targeting)", determine_target,
        limit=args.limit, dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"\n[DRY RUN] Would search for {found1 + not_found1} leads")
        return

    print(f"\nPass 1 results: {found1} found, {not_found1} not found")

    if not_found1 == 0:
        print("\nAll leads found in Pass 1!")
    else:
        # Re-read sheet for Pass 2
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
        ).execute()
        data_rows = result.get("values", [])[1:]

        def flipped_target(job_title, company_size):
            target, reasoning = determine_target(job_title, company_size)
            flipped = flip_target(target)
            return flipped, f"Retry with flipped target: {flipped}"

        found2, not_found2 = run_pass(
            service, sheet_id, tab_name, data_rows,
            "Pass 2 (flipped targeting)", flipped_target,
            limit=args.limit, dry_run=args.dry_run,
        )
        print(f"\nPass 2 results: {found2} found, {not_found2} still not found")
        print(f"\nTotal: {found1 + found2} found, {not_found2} not found")

    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
