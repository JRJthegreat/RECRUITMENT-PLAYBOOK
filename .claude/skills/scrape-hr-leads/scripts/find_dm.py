"""
Phase 1.5: Find decision makers for HR job postings via rules + Apify LinkedIn scraper.

Reads job postings from Google Sheets, determines the target DM title using
rules based on company size + role seniority, then finds the actual person
on LinkedIn using Apify's company employee scraper.
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

# Apify config
APIFY_LINKEDIN_ACTOR = "harvestapi~linkedin-company-employees"
APIFY_SYNC_BASE = "https://api.apify.com/v2/acts"
APIFY_TIMEOUT = 300

MAX_WORKERS = 5

# --- Seniority classification for the role being HIRED ---

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

# --- Target title variations (what to search for on LinkedIn) ---

TARGET_CEO = [
    "CEO", "Chief Executive Officer", "Founder", "Co-Founder",
    "Owner", "President", "COO", "Chief Operating Officer",
    "Managing Partner", "General Manager",
]

TARGET_VP_PEOPLE = [
    "VP of People", "VP of HR", "VP Human Resources",
    "Chief People Officer", "CHRO", "Chief Human Resources Officer",
    "Head of People", "Head of HR", "Head of Human Resources",
    "SVP People", "SVP Human Resources",
    "VP People Operations", "VP Talent",
    "Vice President of People", "Vice President of Human Resources",
]

TARGET_DIRECTOR_HR = [
    "Director of HR", "Director of Human Resources", "HR Director",
    "Director of People", "Director of People Operations",
    "Senior HR Manager", "Head of Recruiting",
    "Director of Talent Acquisition", "Director of Talent",
    "VP Talent Acquisition",
]

TARGET_HR_MANAGER = [
    "HR Manager", "Human Resources Manager",
    "HR Business Partner", "Senior HRBP",
    "People Operations Manager", "Talent Acquisition Manager",
    "Recruiting Manager",
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


def parse_employee_count(count_str):
    """Parse employee count range string to an integer (use upper bound)."""
    if not count_str:
        return None
    s = str(count_str).strip()
    s = s.replace(",", "").replace("+", "")
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def classify_job_seniority(job_title):
    """Classify the role being hired as senior, mid, or junior."""
    title_lower = (job_title or "").lower().strip()
    for keyword in SENIOR_HR_TITLES:
        if keyword in title_lower:
            return "senior"
    for keyword in MID_HR_TITLES:
        if keyword in title_lower:
            return "mid"
    return "junior"


def determine_target_titles(job_title, employee_count_str):
    """
    Determine who the decision maker is based on company size + role seniority.
    Returns (title_variations, target_level, confidence, reasoning).
    """
    seniority = classify_job_seniority(job_title)
    count = parse_employee_count(employee_count_str)

    # Rule 1: Hiring a senior HR leader → target CEO regardless of size
    if seniority == "senior":
        return (
            TARGET_CEO, "ceo", "high",
            f"Hiring senior HR leader ({job_title}), targeting CEO/Founder"
        )

    # Rule 2: Unknown company size → treat as SMB, target CEO
    if count is None:
        return (
            TARGET_CEO, "ceo", "medium",
            f"Unknown company size, defaulting to CEO/Founder"
        )

    # Rule 3: Small company (<200 employees)
    if count < 200:
        return (
            TARGET_CEO, "ceo", "high",
            f"Small company ({count} employees), targeting CEO/Founder"
        )

    # Rule 4: Mid-size (200-1000)
    if count <= 1000:
        conf = "high" if seniority == "mid" else "medium"
        return (
            TARGET_VP_PEOPLE, "vp_people", conf,
            f"Mid-size company ({count} employees), targeting VP People/CHRO"
        )

    # Rule 5: Large (1000+)
    if seniority == "mid":
        return (
            TARGET_VP_PEOPLE + TARGET_DIRECTOR_HR, "vp_or_director", "medium",
            f"Large company ({count} employees), mid role ({job_title}), targeting VP People or Director HR"
        )
    # Junior role at large company
    return (
        TARGET_DIRECTOR_HR + TARGET_HR_MANAGER, "director_or_manager", "medium",
        f"Large company ({count} employees), junior role ({job_title}), targeting Director HR or HR Manager"
    )


def search_linkedin_employees(apify_token, company_linkedin_url, title_variations):
    """Find employees at a company matching target titles via Apify LinkedIn scraper."""
    url = f"{APIFY_SYNC_BASE}/{APIFY_LINKEDIN_ACTOR}/run-sync-get-dataset-items"

    payload = {
        "companies": [company_linkedin_url],
        "jobTitles": title_variations[:20],  # Apify max 20 titles
        "locations": ["United States"],
        "maxItems": 10,
        "profileScraperMode": "Short ($4 per 1k)",
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                params={"token": apify_token, "format": "json"},
                json=payload,
                timeout=APIFY_TIMEOUT,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            elif resp.status_code == 402:
                print(f"    Apify: insufficient credits")
                return []
            else:
                print(f"    Apify error {resp.status_code}: {resp.text[:200]}")
                if attempt == 0:
                    time.sleep(2)
        except requests.exceptions.Timeout:
            print(f"    Apify timeout (attempt {attempt + 1})")
            if attempt == 0:
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            print(f"    Apify request error: {e}")
            return []

    return []


def get_candidate_title(candidate):
    """Extract the current job title from an Apify LinkedIn result."""
    # LinkedIn employee scraper: title is in currentPositions[0]["title"]
    positions = candidate.get("currentPositions") or []
    if positions:
        return positions[0].get("title", "")
    # Google fallback: title is in headline
    return candidate.get("headline") or ""


def pick_best_match(candidates, title_variations):
    """Pick the best candidate from Apify results based on title priority."""
    if not candidates:
        return None, "low"

    title_lower_list = [t.lower() for t in title_variations]

    scored = []
    for candidate in candidates:
        title = get_candidate_title(candidate).lower()
        best_score = 999
        for i, target in enumerate(title_lower_list):
            if target.lower() in title:
                best_score = min(best_score, i)
        scored.append((best_score, candidate))

    scored.sort(key=lambda x: x[0])
    best_score, best = scored[0]

    if best_score == 999:
        # No title match — return first result with low confidence
        return best, "low"
    elif best_score <= 2:
        return best, "high"
    else:
        return best, "medium"


def process_lead(lead, apify_token):
    """Process a single lead: rules → Apify → match → result."""
    titles, level, base_confidence, reasoning = determine_target_titles(
        lead["job_title"], lead["employee_count"]
    )

    candidates = []
    method = "linkedin"

    if lead["company_linkedin_url"]:
        candidates = search_linkedin_employees(
            apify_token, lead["company_linkedin_url"], titles
        )
    else:
        method = "no_linkedin_url"

    match, match_confidence = pick_best_match(candidates, titles)

    # Combine base confidence with match confidence
    # If match confidence is lower, downgrade
    conf_order = {"high": 3, "medium": 2, "low": 1}
    final_confidence = min(conf_order.get(base_confidence, 2), conf_order.get(match_confidence, 1))
    conf_labels = {3: "high", 2: "medium", 1: "low"}
    confidence = conf_labels[final_confidence]

    if match:
        first = match.get("firstName", "")
        last = match.get("lastName", "")
        person_name = f"{first} {last}".strip()
        result_title = get_candidate_title(match)
        linkedin_url = match.get("linkedinUrl", "")
        reasoning += f" | Found via {method}: {person_name} ({result_title})"
    else:
        person_name = ""
        result_title = ""
        linkedin_url = ""
        confidence = "low"
        reasoning += f" | No match found via {method}"

    return {
        "row_num": lead["row_num"],
        "person_name": person_name,
        "result_title": result_title,
        "linkedin_url": linkedin_url,
        "dm_confidence": confidence,
        "dm_reasoning": reasoning,
    }


def col_letter(idx):
    """Convert 0-based column index to sheet letter (0=A, 25=Z, 26=AA, etc.)."""
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def main():
    parser = argparse.ArgumentParser(description="Find decision makers for HR job postings")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview rules output without calling Apify")
    args = parser.parse_args()

    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token and not args.dry_run:
        print("Error: APIFY_API_TOKEN not set in .env")
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

    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    # Required columns
    idx_person = col_idx("person_name")
    idx_result_title = col_idx("result_title")
    idx_linkedin = col_idx("linkedin_url")
    idx_company_name = col_idx("company name")
    idx_job_title = col_idx("job_title")
    idx_company_linkedin = col_idx("company_linkedin_url")
    idx_company_url = col_idx("company_url")
    idx_employee_count = col_idx("company_employee_count")
    idx_dm_confidence = col_idx("dm_confidence")
    idx_dm_reasoning = col_idx("dm_reasoning")

    missing = []
    for name, idx in [
        ("person_name", idx_person), ("company name", idx_company_name),
        ("job_title", idx_job_title), ("dm_confidence", idx_dm_confidence),
    ]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        print(f"Available: {headers}")
        sys.exit(1)

    # Collect rows needing DM lookup
    leads = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        # Skip if already has person_name
        if cell(idx_person):
            continue

        company_name = cell(idx_company_name)
        job_title = cell(idx_job_title)

        # Skip if no company name or no job title
        if not company_name or not job_title:
            continue

        leads.append({
            "row_num": i,
            "company_name": company_name,
            "job_title": job_title,
            "company_linkedin_url": cell(idx_company_linkedin),
            "company_url": cell(idx_company_url),
            "employee_count": cell(idx_employee_count),
        })

        if args.limit and len(leads) >= args.limit:
            break

    if not leads:
        print("No leads need DM lookup (all rows already have person_name)")
        sys.exit(0)

    print(f"\nFound {len(leads)} leads to process")

    # Count how many have LinkedIn URLs
    with_linkedin = sum(1 for l in leads if l["company_linkedin_url"])
    print(f"  {with_linkedin} with company LinkedIn URL (Apify scraper)")
    print(f"  {len(leads) - with_linkedin} without (Google search fallback)")

    # Dry run: just show rules output
    if args.dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — Rules output (no Apify calls)\n")
        for lead in leads[:20]:
            titles, level, confidence, reasoning = determine_target_titles(
                lead["job_title"], lead["employee_count"]
            )
            print(f"  Row {lead['row_num']}: {lead['company_name']}")
            print(f"    Hiring: {lead['job_title']} | Employees: {lead['employee_count'] or '?'}")
            print(f"    Target: {level} → {', '.join(titles[:4])}...")
            print(f"    Confidence: {confidence} | {reasoning}")
            print(f"    Method: {'LinkedIn scraper' if lead['company_linkedin_url'] else 'Google fallback'}")
            print()
        if len(leads) > 20:
            print(f"  ... and {len(leads) - 20} more")
        print(f"{'='*70}")
        sys.exit(0)

    # Process leads in batches of 10
    BATCH_SIZE = 10
    total_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nProcessing {len(leads)} leads in batches of {BATCH_SIZE} ({total_batches} batches, {MAX_WORKERS} workers)...\n")
    results = []
    found = 0
    not_found = 0

    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch = leads[batch_start:batch_start + BATCH_SIZE]
        batch_results = []

        print(f"--- Batch {batch_num + 1}/{total_batches} (rows {batch[0]['row_num']}-{batch[-1]['row_num']}) ---")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_lead = {
                executor.submit(process_lead, lead, apify_token): lead
                for lead in batch
            }
            for future in as_completed(future_to_lead):
                lead = future_to_lead[future]
                try:
                    result = future.result()
                    batch_results.append(result)
                    results.append(result)
                    if result["person_name"]:
                        found += 1
                        print(f"  Row {result['row_num']}: {result['person_name']} ({result['result_title'][:50]}) [{result['dm_confidence']}]")
                    else:
                        not_found += 1
                        print(f"  Row {result['row_num']}: No match — {lead['company_name']} [{result['dm_confidence']}]")
                except Exception as e:
                    not_found += 1
                    print(f"  Row {lead['row_num']}: Error — {e}")
                    batch_results.append({
                        "row_num": lead["row_num"],
                        "person_name": "",
                        "result_title": "",
                        "linkedin_url": "",
                        "dm_confidence": "low",
                        "dm_reasoning": f"Error: {e}",
                    })
                    results.append(batch_results[-1])

        # Write this batch to sheet immediately
        if batch_results:
            updates = []
            for r in batch_results:
                row = r["row_num"]
                updates.append({
                    "range": f"{col_letter(idx_person)}{row}",
                    "values": [[r["person_name"]]],
                })
                if idx_result_title is not None:
                    updates.append({
                        "range": f"{col_letter(idx_result_title)}{row}",
                        "values": [[r["result_title"]]],
                    })
                if idx_linkedin is not None:
                    updates.append({
                        "range": f"{col_letter(idx_linkedin)}{row}",
                        "values": [[r["linkedin_url"]]],
                    })
                if idx_dm_confidence is not None:
                    updates.append({
                        "range": f"{col_letter(idx_dm_confidence)}{row}",
                        "values": [[r["dm_confidence"]]],
                    })
                if idx_dm_reasoning is not None:
                    updates.append({
                        "range": f"{col_letter(idx_dm_reasoning)}{row}",
                        "values": [[r["dm_reasoning"]]],
                    })

            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"  → Written to sheet. Running total: {found} found, {not_found} not found\n")

    # Summary
    print(f"\n{'='*50}")
    print(f"Find DM Complete")
    print(f"  Decision makers found: {found}")
    print(f"  Not found: {not_found}")
    print(f"  Total processed: {found + not_found}")
    high = sum(1 for r in results if r["dm_confidence"] == "high")
    med = sum(1 for r in results if r["dm_confidence"] == "medium")
    low = sum(1 for r in results if r["dm_confidence"] == "low")
    print(f"  Confidence: {high} high, {med} medium, {low} low")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
