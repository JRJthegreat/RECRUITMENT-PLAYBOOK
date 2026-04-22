"""
Phase 2: Find tech decision makers via Google Search + LinkedIn

Rules (user-defined for tech recruitment):
  <50 employees           → Pass 1: CEO/Founder, Pass 2: CTO
  50-200 employees        → Pass 1: CTO,         Pass 2: CEO/Founder
  200-500 + C-level role  → Pass 1: CEO/Founder, Pass 2: CTO
  200-500 + other role    → Pass 1: HR (TA/HR Director/Head of People), Pass 2: CTO
  >500 employees          → Should never reach Phase 2 (filtered at pull_dataset)
                            but if present, also skipped here
  Unknown size            → Pass 1: CTO, Pass 2: CEO/Founder

Each pass:
  1. Build Google Search query: "{company}" ("{title vars}") site:linkedin.com/in/
  2. Run Apify Google Search Scraper (batched)
  3. Parse LinkedIn snippet → person_name + title
  4. Validate title against target keyword set
  5. Reject noise via is_leadership_title (assistant, intern, etc.)
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
MAX_EMPLOYEES = 500

# --- Column indices (0-based, matching HEADERS in pull_dataset.py) ---
COL_JOB_TITLE = 1       # B: Job Title
COL_COMPANY_NAME = 10   # K: Company Name
COL_COMPANY_SIZE = 12   # M: Company Size
COL_DM_NAME = 19        # T: DM Name
COL_DM_TITLE = 20       # U: DM Title
COL_LINKEDIN_URL = 21   # V: LinkedIn URL

# --- C-level detection (triggers "200-500 + C-level → CEO" branch) ---
C_LEVEL_KEYWORDS = [
    r"\bcto\b", r"\bcio\b", r"\bciso\b", r"\bcdo\b", r"\bcoo\b", r"\bceo\b",
    r"\bcpo\b", r"\bcfo\b", r"\bchro\b", r"\bcaio\b",
    r"\bchief\s+technolog", r"\bchief\s+technical", r"\bchief\s+information",
    r"\bchief\s+data", r"\bchief\s+operating", r"\bchief\s+executive",
    r"\bchief\s+product", r"\bchief\s+ai\b", r"\bchief\s+architect",
    r"\bvp\s+of\s+engineering", r"\bvp\s+engineering",
    r"\bvice\s+president\s+of\s+engineering",
    r"\bhead\s+of\s+engineering",
]


def is_c_level_role(job_title):
    t = (job_title or "").lower().strip()
    if not t:
        return False
    return any(re.search(p, t) for p in C_LEVEL_KEYWORDS)


# --- Employee count parsing ---

def parse_employee_count(count_str):
    """Returns the upper bound of the range as int, or None if unparseable.
    '11 to 50' → 50, '201 to 500' → 500, '10,000+' → 10000."""
    if not count_str:
        return None
    s = str(count_str).strip().replace(",", "").replace("+", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    if not s:
        return None
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def parse_employee_count_lower(count_str):
    """Lower bound of the range, used for the >500 skip check
    (matches pull_dataset.py's filter logic)."""
    if not count_str:
        return None
    s = str(count_str).strip().replace(",", "").replace("+", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    if not s:
        return None
    try:
        return int(s.split("-")[0])
    except (ValueError, IndexError):
        return None


# --- DM targeting rules (user-defined for tech) ---

def determine_target(job_title, employee_count_str):
    """Returns ('ceo'|'cto'|'hr', reasoning)."""
    count = parse_employee_count(employee_count_str)

    if count is None:
        return "cto", "Unknown company size, defaulting to CTO"

    if count < 50:
        return "ceo", f"Small company ({count} employees), targeting CEO/Founder"

    if count <= 200:
        return "cto", f"Mid-small company ({count} employees), targeting CTO"

    if count <= 500:
        if is_c_level_role(job_title):
            return "ceo", f"Hiring C-level ({job_title}) at mid-size company ({count}), targeting CEO/Founder"
        return "hr", f"Mid-size company ({count} employees), non-C-level role, targeting HR/TA"

    # Should not reach here (filtered upstream); fall back to CTO if it does
    return "cto", f"Large company ({count} employees) — should have been filtered upstream"


def flip_target(target):
    """Auto-retry: every category flips to CTO except CTO itself, which flips to CEO."""
    if target == "cto":
        return "ceo"
    return "cto"


# --- Google Search query builders ---

def build_search_query(company_name, target_level):
    if target_level == "ceo":
        return (
            f'"{company_name}" '
            f'("CEO" OR "Founder" OR "Co-Founder" OR "Owner" OR "Managing Director" OR "President") '
            f'site:linkedin.com/in/'
        )
    if target_level == "cto":
        return (
            f'"{company_name}" '
            f'("CTO" OR "Chief Technology Officer" OR "VP Engineering" OR "VP of Engineering" OR "Head of Engineering") '
            f'site:linkedin.com/in/'
        )
    if target_level == "hr":
        return (
            f'"{company_name}" '
            f'("Talent Acquisition" OR "HR Director" OR "Director of HR" OR "Head of People" OR "Head of HR" '
            f'OR "Head of Talent" OR "VP People" OR "VP HR" OR "Chief People Officer") '
            f'site:linkedin.com/in/'
        )
    return f'"{company_name}" "CTO" site:linkedin.com/in/'


# --- LinkedIn snippet parsing ---

def parse_linkedin_result(organic_result):
    """Extract person name and title from a LinkedIn search result."""
    title = organic_result.get("title", "")
    url = organic_result.get("url", "")

    if "linkedin.com/in/" not in url:
        return None, None, None

    # Strip " | LinkedIn" suffix
    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()

    # "Name - Title - Company" or "Name – Title at Company"
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    if len(parts) >= 2:
        person_name = parts[0].strip()
        result_title = parts[1].strip()
        result_title = re.sub(r"\s+at\s+.*$", "", result_title, flags=re.IGNORECASE).strip()
        return person_name, result_title, url

    # "Name, Title"
    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url

    return None, None, url


# --- Validation ---

# Hard rejects: even if "CTO" is in the title, if the person's primary role is
# Assistant/Intern/etc., they're not the DM. Ported from scrape-tech-leads.
HARD_REJECT_PATTERNS = [
    r"\bassistant\b", r"\bsecretary\b",
    r"\bintern\b", r"\bstudent\b", r"\bfellow\b",
    r"\bcontractor\b", r"\bfreelance",
    r"\bresearcher\b", r"\bresearch associate\b", r"\bpostdoc", r"\blecturer\b",
    r"\bretired\b", r"\bin pension\b", r"\bformer\b",
    r"\boffice of\b",
    r"\bproduct owner\b", r"\bplatform owner\b", r"\bprocess owner\b", r"\bdata owner\b",
]


def is_leadership_title(title):
    """Reject obvious noise. Mirrors scrape-tech-leads/find_dm.py."""
    t = (title or "").strip()
    if not t:
        return False
    primary = re.split(r"[,|;]", t)[0].strip().lower()
    primary_is_leader = bool(re.match(
        r"(?:co-?)?(?:ceo|cto|coo|cio|cpo|chro|vp|founder|director|head|managing director|"
        r"chief (?:executive|technology|operating|information|people|product|data|architect))",
        primary, re.IGNORECASE,
    ))
    if not primary_is_leader:
        for pat in HARD_REJECT_PATTERNS:
            if re.search(pat, primary, re.IGNORECASE):
                return False
    return True


def validate_result(person_name, result_title, target_level):
    """Validate that the search result matches the target category."""
    if not person_name or not result_title:
        return False
    if not is_leadership_title(result_title):
        return False

    title_lower = result_title.lower()

    if target_level == "ceo":
        return any(kw in title_lower for kw in [
            "ceo", "founder", "owner", "managing director", "president",
            "co-founder", "cofounder", "chief executive",
        ])

    if target_level == "cto":
        return any(kw in title_lower for kw in [
            "cto", "chief technology", "chief technical",
            "vp engineering", "vp of engineering", "vice president of engineering",
            "head of engineering",
        ])

    if target_level == "hr":
        ta_kw = ["talent acquisition", "hr ", " hr", "human resources", "people", "talent"]
        rank_kw = ["director", "head of", "vp", "vice president", "chief", "manager"]
        has_ta = any(kw in title_lower for kw in ta_kw)
        has_rank = any(kw in title_lower for kw in rank_kw)
        return has_ta and has_rank

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
    """Run Apify Google Search Scraper. Returns dict {query: [organic_results]}."""
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
    """Process a batch: build queries, search, parse, write."""
    queries = []
    query_to_lead = {}

    for lead in leads:
        target = target_map[lead["sheet_row"]]
        query = build_search_query(lead["company_name"], target)
        queries.append(query)
        query_to_lead[query] = lead

    if dry_run:
        for query, lead in query_to_lead.items():
            target = target_map[lead["sheet_row"]]
            print(f"  [DRY RUN] Row {lead['sheet_row']}: {lead['company_name']} ({lead['employee_count']}) "
                  f"hiring {lead['job_title']!r} → {target}")
        return 0, len(leads)

    search_results = apify_google_search(queries)

    updates = []
    found = 0
    not_found = 0

    for query, lead in query_to_lead.items():
        target = target_map[lead["sheet_row"]]
        organic = search_results.get(query, [])

        matched = False
        for result in organic[:5]:
            person_name, result_title, linkedin_url = parse_linkedin_result(result)
            if person_name and validate_result(person_name, result_title, target):
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


def collect_leads(rows, target_fn, limit=0):
    """Walk sheet rows and build the work queue."""
    leads = []
    target_map = {}
    skipped_too_big = 0

    for i, row in enumerate(rows):
        if limit > 0 and len(leads) >= limit:
            break

        if cell(row, COL_DM_NAME):
            continue

        company_name = cell(row, COL_COMPANY_NAME)
        if not company_name:
            continue

        company_size = cell(row, COL_COMPANY_SIZE)
        size_lower = parse_employee_count_lower(company_size)
        if size_lower is not None and size_lower > MAX_EMPLOYEES:
            skipped_too_big += 1
            continue

        job_title = cell(row, COL_JOB_TITLE)
        target, _ = target_fn(job_title, company_size)
        sheet_row = i + 2

        leads.append({
            "sheet_row": sheet_row,
            "company_name": company_name,
            "job_title": job_title,
            "employee_count": company_size,
        })
        target_map[sheet_row] = target

    if skipped_too_big:
        print(f"  Skipped {skipped_too_big} rows (>{MAX_EMPLOYEES} employees)")

    return leads, target_map


def run_pass(service, sheet_id, tab_name, rows, pass_name, target_fn, limit=0, dry_run=False):
    leads, target_map = collect_leads(rows, target_fn, limit=limit)

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
    parser = argparse.ArgumentParser(description="Find tech decision makers via Google Search + LinkedIn")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--limit", type=int, default=0, help="Max leads per pass (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview targeting rules without searching")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Find Tech Decision Makers (Google Search + LinkedIn) ===\n")

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
    data_rows = all_rows[1:]

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
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AC"
        ).execute()
        data_rows = result.get("values", [])[1:]

        def flipped_target(job_title, company_size):
            target, _ = determine_target(job_title, company_size)
            flipped = flip_target(target)
            return flipped, f"Retry with flipped target: {flipped}"

        found2, not_found2 = run_pass(
            service, sheet_id, tab_name, data_rows,
            "Pass 2 (flipped targeting)", flipped_target,
            limit=args.limit, dry_run=False,
        )
        print(f"\nPass 2 results: {found2} found, {not_found2} still not found")
        print(f"\nTotal: {found1 + found2} found, {not_found2} not found")

    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
