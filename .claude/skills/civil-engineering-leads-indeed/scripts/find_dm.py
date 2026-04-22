"""
Phase 2: Find civil engineering decision makers via Google Search + LinkedIn.

REQUIRES col L (Company Website) to be populated first via find_company_domains.py.
Rows without a verified domain are skipped — we won't outreach without the
domain-anchored verification step.

Rules (UK civil engineering / construction firms):
  <50 employees           → Pass 1: Owner/MD/CEO,            Pass 2: COO/Ops Director
  50-200 employees        → Pass 1: COO/Ops Director,        Pass 2: MD/CEO
  200-500 + C-level role  → Pass 1: MD/CEO,                  Pass 2: COO
  200-500 + other role    → Pass 1: COO/Ops Director,        Pass 2: MD/CEO
  >500 employees          → Should never reach Phase 2 (filtered at pull_dataset)
  Unknown size            → Pass 1: COO/Ops Director,        Pass 2: MD/CEO

Each pass:
  1. Build Google Search query: "{company}" ("{title vars}") site:linkedin.com/in/
  2. Run Apify Google Search Scraper (batched)
  3. Parse LinkedIn snippet → person_name, title, employer-from-snippet
  4. Validate: title matches target keywords AND snippet mentions the
     target company OR target domain (verifies person actually works there).
  5. Reject noise via is_leadership_title (assistant, intern, etc.)
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

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

BATCH_SIZE = 25
SHEET_WRITE_DELAY = 0.5
BATCH_WORKERS = 5
MAX_EMPLOYEES = 500

# --- Column indices (0-based, matching HEADERS in pull_dataset.py) ---
COL_JOB_TITLE = 1       # B: Job Title
COL_COMPANY_NAME = 10   # K: Company Name
COL_COMPANY_WEBSITE = 11  # L: Company Website (verified domain, from find_company_domains.py)
COL_COMPANY_SIZE = 12   # M: Company Size
COL_DM_NAME = 19        # T: DM Name
COL_DM_TITLE = 20       # U: DM Title
COL_LINKEDIN_URL = 21   # V: LinkedIn URL

# --- C-level detection (triggers "200-500 + C-level → MD/CEO" branch) ---
# UK terms: MD = Managing Director (== US CEO/President)
C_LEVEL_KEYWORDS = [
    r"\bceo\b", r"\bcoo\b", r"\bcfo\b", r"\bcio\b", r"\bcto\b",
    r"\bchro\b", r"\bcpo\b",
    r"\bchief\s+executive", r"\bchief\s+operating", r"\bchief\s+financial",
    r"\bchief\s+information", r"\bchief\s+technical", r"\bchief\s+technolog",
    r"\bmanaging\s+director\b", r"\bmd\b", r"\bpresident\b",
    r"\bengineering\s+director\b", r"\bdirector\s+of\s+engineering\b",
    r"\boperations\s+director\b", r"\bdirector\s+of\s+operations\b",
    r"\bvp\s+of\s+engineering\b", r"\bvp\s+engineering\b",
    r"\bhead\s+of\s+engineering\b",
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
    """Lower bound of the range, used for the >1000 skip check
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


# --- DM targeting rules (UK civil engineering) ---

def determine_target(job_title, employee_count_str):
    """Returns ('owner'|'coo'|'hr', reasoning)."""
    count = parse_employee_count(employee_count_str)

    if count is None:
        return "coo", "Unknown company size, defaulting to COO/Ops Director"

    if count < 50:
        return "owner", f"Small firm ({count} employees), targeting MD/Owner/CEO"

    if count <= 200:
        return "coo", f"Mid-small firm ({count} employees), targeting COO/Ops Director"

    if count <= 500:
        if is_c_level_role(job_title):
            return "owner", f"Hiring C-level ({job_title}) at {count}-employee firm, targeting MD/CEO"
        return "coo", f"Mid firm ({count} employees), normal role, targeting COO/Ops Director"

    # Should not reach here (filtered upstream); fall back to COO if it does
    return "coo", f"Larger firm ({count} employees) — should have been filtered upstream"


def fallback_target(pass1_target, employee_count_str):
    """Pass 2 fallback, size-aware:
      <200 employees       → CEO/MD/owner (or COO if Pass 1 already hit owner)
      200+ employees       → HR Director / Head of TA / VP People
      Unknown size         → CEO/MD/owner
    Returns None if the fallback would be the same as Pass 1 (skip the pass)."""
    count = parse_employee_count(employee_count_str)
    if count is not None and count >= 200:
        fallback = "hr"
    else:
        fallback = "owner"

    # If Pass 1 already targeted this level, try the opposite (COO) instead of
    # burning a second identical search.
    if fallback == pass1_target:
        if pass1_target == "owner":
            return "coo"
        return None  # already tried hr or coo — no further fallback

    return fallback


# --- Google Search query builders ---

def build_search_query(company_name, target_level):
    if target_level == "owner":
        return (
            f'"{company_name}" '
            f'("Managing Director" OR "MD" OR "Owner" OR "Founder" OR "Co-Founder" OR "CEO" OR "President") '
            f'site:linkedin.com/in/'
        )
    if target_level == "coo":
        return (
            f'"{company_name}" '
            f'("COO" OR "Chief Operating Officer" OR "Operations Director" OR "Director of Operations" '
            f'OR "VP Operations" OR "VP of Operations" OR "Head of Operations") '
            f'site:linkedin.com/in/'
        )
    if target_level == "hr":
        return (
            f'"{company_name}" '
            f'("HR Director" OR "Head of HR" OR "Head of People" OR "People Director" '
            f'OR "VP of People" OR "VP People" OR "Chief People Officer" OR "CHRO" '
            f'OR "Talent Acquisition Director" OR "Head of Talent Acquisition" OR "Head of Recruitment") '
            f'site:linkedin.com/in/'
        )
    return f'"{company_name}" "Operations Director" site:linkedin.com/in/'


# --- LinkedIn snippet parsing ---

def _name_words(company_name):
    """Normalize a company name into its identifying word tokens.
    Drops legal/boilerplate suffixes so overlap matching isn't skewed."""
    noise = {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&",
             "a", "an", "for", "in", "at", "by", "uk", "plc", "llp",
             "group", "holdings", "limited", "company"}
    words = re.split(r"[\s,.\-&/()+]+", (company_name or "").lower())
    return [w for w in words if len(w) > 2 and w not in noise]


def parse_linkedin_result(organic_result):
    """Extract (person_name, role_title, url, employer_hint, description).
    employer_hint is the company segment after 'at X' or the 3rd `-` segment,
    if present. description is the full search-result snippet body.
    Returns (None, None, None, "", "") when the result isn't a LinkedIn /in/ profile."""
    title = organic_result.get("title", "")
    url = organic_result.get("url", "")
    description = organic_result.get("description", "") or organic_result.get("snippet", "")

    if "linkedin.com/in/" not in url:
        return None, None, None, "", ""

    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()

    employer_hint = ""
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    person_name = ""
    result_title = ""

    if len(parts) >= 2:
        person_name = parts[0].strip()
        result_title = parts[1].strip()
        # "Title at Company" → peel employer off the role title
        m = re.search(r"\s+at\s+(.+)$", result_title, flags=re.IGNORECASE)
        if m:
            employer_hint = m.group(1).strip()
            result_title = result_title[:m.start()].strip()
        elif len(parts) >= 3:
            employer_hint = parts[2].strip()
        return person_name, result_title, url, employer_hint, description

    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url, "", description

    return None, None, url, "", description


def verify_employer(employer_hint, description, target_company, target_domain):
    """Confirm this LinkedIn profile is actually for the target company.
    Matches if ≥half the target's name-words appear in the snippet OR the
    domain appears literally. Returns False when we can't find any evidence."""
    words = _name_words(target_company)
    haystack = " ".join(filter(None, [employer_hint, description])).lower()
    if not haystack:
        return False

    if target_domain:
        dom = target_domain.lower().strip()
        if dom and dom in haystack:
            return True
        # Also accept the bare label (e.g. "crownhighways" from "crownhighways.co.uk")
        label = dom.split(".")[0]
        if len(label) >= 4 and label in haystack:
            return True

    if not words:
        return False
    matches = sum(1 for w in words if w in haystack)
    return matches >= max(1, len(words) // 2)


# --- Validation ---

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
    """Reject obvious noise. Same pattern as tech-leads-indeed but adds MD/Ops Director."""
    t = (title or "").strip()
    if not t:
        return False
    primary = re.split(r"[,|;]", t)[0].strip().lower()
    primary_is_leader = bool(re.match(
        r"(?:co-?)?(?:ceo|coo|cfo|cio|cto|chro|cpo|md|vp|founder|owner|director|head|"
        r"managing director|president|hr director|hr manager|people director|"
        r"chief (?:executive|operating|financial|technology|operating|information|people|product))",
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

    if target_level == "owner":
        return any(kw in title_lower for kw in [
            "managing director", " md ", "md,", "md ", "ceo", "founder", "owner",
            "co-founder", "cofounder", "president", "chief executive",
        ]) or title_lower.strip() in ("md", "ceo")

    if target_level == "coo":
        return any(kw in title_lower for kw in [
            "coo", "chief operating", "operations director", "director of operations",
            "ops director", "vp operations", "vp of operations", "head of operations",
        ])

    if target_level == "hr":
        return any(kw in title_lower for kw in [
            "hr director", "head of hr", "director of hr",
            "head of people", "people director", "director of people",
            "vp of people", "vp people", "chief people", "chro",
            "head of talent", "talent acquisition director", "director of talent",
            "head of recruitment", "recruitment director",
        ])

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
            "countryCode": "gb",
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
    """Process a batch: build queries, search, parse, verify, write."""
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
                  f"hiring {lead['job_title']!r} → {target}  domain={lead['domain']}")
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
            person_name, result_title, linkedin_url, employer_hint, description = parse_linkedin_result(result)
            if not person_name or not validate_result(person_name, result_title, target):
                continue
            if not verify_employer(employer_hint, description, lead["company_name"], lead["domain"]):
                continue
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
            print(f"    Row {lead['sheet_row']}: {lead['company_name']} → NOT FOUND (no verified match)")
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
    """Walk sheet rows and build the work queue.
    Skips rows with no verified domain (col L) — we require the domain
    anchor for DM verification."""
    leads = []
    target_map = {}
    skipped_too_big = 0
    skipped_no_domain = 0

    for i, row in enumerate(rows):
        if limit > 0 and len(leads) >= limit:
            break

        if cell(row, COL_DM_NAME):
            continue

        company_name = cell(row, COL_COMPANY_NAME)
        if not company_name:
            continue

        domain = cell(row, COL_COMPANY_WEBSITE)
        if not domain or "." not in domain:
            skipped_no_domain += 1
            continue

        company_size = cell(row, COL_COMPANY_SIZE)
        size_lower = parse_employee_count_lower(company_size)
        if size_lower is not None and size_lower > MAX_EMPLOYEES:
            skipped_too_big += 1
            continue

        job_title = cell(row, COL_JOB_TITLE)
        target, _ = target_fn(job_title, company_size)
        if not target:
            continue  # fallback_target returned None — nothing further to try
        sheet_row = i + 2

        leads.append({
            "sheet_row": sheet_row,
            "company_name": company_name,
            "domain": domain,
            "job_title": job_title,
            "employee_count": company_size,
        })
        target_map[sheet_row] = target

    if skipped_too_big:
        print(f"  Skipped {skipped_too_big} rows (>{MAX_EMPLOYEES} employees)")
    if skipped_no_domain:
        print(f"  Skipped {skipped_no_domain} rows (no verified domain in col L)")

    return leads, target_map


def run_pass(service, sheet_id, tab_name, rows, pass_name, target_fn, limit=0, dry_run=False):
    leads, target_map = collect_leads(rows, target_fn, limit=limit)

    print(f"\n{pass_name}: {len(leads)} leads to process")
    if not leads:
        return 0, 0

    total_found = 0
    total_not_found = 0
    num_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    batches = [leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE] for b in range(num_batches)]

    if dry_run:
        for b, batch in enumerate(batches, 1):
            print(f"  Batch {b}/{num_batches}")
            found, not_found = process_batch(service, sheet_id, tab_name, batch, target_map, dry_run=True)
            total_found += found
            total_not_found += not_found
        return total_found, total_not_found

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        futures = {pool.submit(process_batch, service, sheet_id, tab_name, batch, target_map, False): i
                   for i, batch in enumerate(batches, 1)}
        done = 0
        for fut in as_completed(futures):
            b = futures[fut]
            done += 1
            try:
                found, not_found = fut.result()
            except Exception as e:
                print(f"  Batch {b}/{num_batches}: EXC {e}")
                continue
            total_found += found
            total_not_found += not_found
            print(f"  [{done}/{num_batches} batches complete] found={total_found} not_found={total_not_found}")

    return total_found, total_not_found


def main():
    parser = argparse.ArgumentParser(description="Find civil engineering decision makers via Google Search + LinkedIn")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--limit", type=int, default=0, help="Max leads per pass (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview targeting rules without searching")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Find Civil Engineering Decision Makers (Google Search + LinkedIn) ===\n")

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

        def fallback_target_fn(job_title, company_size):
            pass1, _ = determine_target(job_title, company_size)
            fb = fallback_target(pass1, company_size)
            return fb, f"Fallback (pass1={pass1}): {fb}"

        found2, not_found2 = run_pass(
            service, sheet_id, tab_name, data_rows,
            "Pass 2 (size-aware fallback)", fallback_target_fn,
            limit=args.limit, dry_run=False,
        )
        print(f"\nPass 2 results: {found2} found, {not_found2} still not found")
        print(f"\nTotal: {found1 + found2} found, {not_found2} not found")

    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
