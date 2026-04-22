"""
Phase 1.5: Find decision makers + emails for HR job postings via AnyMail Finder.

Pass 1: For each lead without a person_name or email, call the AnyMail Finder
        decision-maker endpoint using rules based on company size + role seniority.
        Writes person_name, result_title, linkedin_url, email, dm_confidence, dm_reasoning.

Pass 2 (auto-retry): After Pass 1, automatically retries:
  - Rows with person_name but no email  → /find-email/person endpoint
  - Rows with no person_name (not found) → flip the DM category (CEO→HR, HR→CEO)

Both passes write to sheet in batches of 10 with 1.5s delay (60 writes/min limit).
"""

import os
import sys
import re
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

AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
MAX_WORKERS = 5
BATCH_SIZE = 10
SHEET_WRITE_DELAY = 1.5  # seconds between sheet batch writes

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


def parse_employee_count(count_str):
    """Parse employee count range string to an integer (use upper bound).
    Handles formats: '201-500', '201 to 500', '10,000+', '500'
    """
    if not count_str:
        return None
    s = str(count_str).strip().replace(",", "").replace("+", "")
    # Normalise "201 to 500" → "201-500"
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    # Strip trailing non-numeric words (e.g. "employees")
    s = re.sub(r"[^\d\-].*$", "", s).strip()
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


def determine_dm_category(job_title, employee_count_str):
    """
    Determine AnyMail Finder decision_maker_category based on company size + role seniority.
    Returns (categories, confidence, reasoning).
    """
    seniority = classify_job_seniority(job_title)
    count = parse_employee_count(employee_count_str)

    # Rule 1: Hiring a senior HR leader → target CEO regardless of size
    if seniority == "senior":
        return (
            ["ceo"], "high",
            f"Hiring senior HR leader ({job_title}), targeting CEO/Founder"
        )

    # Rule 2: Unknown company size → treat as SMB, target CEO
    if count is None:
        return (
            ["ceo"], "medium",
            f"Unknown company size, defaulting to CEO/Founder"
        )

    # Rule 3: Small company (<200 employees)
    if count < 200:
        return (
            ["ceo"], "high",
            f"Small company ({count} employees), targeting CEO/Founder"
        )

    # Rule 4: Mid-size (200-1000)
    if count <= 1000:
        conf = "high" if seniority == "mid" else "medium"
        return (
            ["hr"], conf,
            f"Mid-size company ({count} employees), targeting HR decision maker"
        )

    # Rule 5: Large (1000+)
    return (
        ["hr"], "medium",
        f"Large company ({count} employees), targeting HR decision maker"
    )


def flip_category(categories):
    """Flip the DM category for retry: CEO→HR, HR→CEO."""
    if "ceo" in categories:
        return ["hr"]
    return ["ceo"]


def find_dm(api_key, company_domain, company_name, dm_categories):
    """Call AnyMail Finder decision-maker endpoint. Returns name, email, title, linkedin."""
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
        return {
            "email": email if email and status in ("valid", "risky") else None,
            "status": status or "not_found",
            "person_name": data.get("person_full_name", ""),
            "person_title": data.get("person_job_title", ""),
            "person_linkedin": data.get("person_linkedin_url", ""),
        }
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"email": None, "status": f"error: {e}"}


def find_email_person(api_key, full_name, company_domain, company_name):
    """Find email for a known person via AnyMail Finder person endpoint."""
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    parts = full_name.strip().split()
    body = {}
    if full_name:
        body["full_name"] = full_name
    if len(parts) >= 1:
        body["first_name"] = parts[0]
    if len(parts) >= 2:
        body["last_name"] = " ".join(parts[1:])
    if company_domain:
        body["domain"] = company_domain
    if company_name:
        body["company_name"] = company_name

    if not full_name or (not company_domain and not company_name):
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
    except Exception as e:
        return {"email": None, "status": f"error: {e}"}


def process_lead(api_key, lead):
    """Pass 1: rules → AnyMail DM endpoint → result dict."""
    categories, confidence, reasoning = determine_dm_category(
        lead["job_title"], lead["employee_count"]
    )

    result = find_dm(api_key, lead["company_url"], lead["company_name"], categories)

    if result.get("person_name"):
        reasoning += f" | Found: {result['person_name']} ({result.get('person_title', '')})"
        if result.get("email"):
            reasoning += f" | Email: {result['email']}"
    else:
        confidence = "low"
        reasoning += f" | No match ({result['status']})"

    return {
        "row_num": lead["row_num"],
        "person_name": result.get("person_name", ""),
        "result_title": result.get("person_title", ""),
        "linkedin_url": result.get("person_linkedin", ""),
        "email": result.get("email", ""),
        "dm_confidence": confidence,
        "dm_reasoning": reasoning,
    }


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
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def write_batch_to_sheet(service, sheet_id, updates, token_path, batch_num=None):
    """Write a batchUpdate to the sheet with 3 retries. Returns True on success."""
    if not updates:
        return True
    for attempt in range(3):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            return True
        except Exception as e:
            is_rate_limit = "429" in str(e) or "RATE_LIMIT" in str(e)
            wait = 30 if is_rate_limit else 5
            label = f"batch {batch_num}" if batch_num is not None else "batch"
            if attempt < 2:
                print(f"  Sheet write retry {attempt + 1} for {label} (waiting {wait}s)... ({e})")
                time.sleep(wait)
                try:
                    service = get_google_service(token_path)
                except Exception:
                    pass
            else:
                print(f"  Sheet write FAILED for {label}: {e}")
                return False
    return False


def run_pass(api_key, leads, pass_name, mode, service, sheet_id,
             idx_person, idx_result_title, idx_linkedin, idx_email,
             idx_dm_confidence, idx_dm_reasoning, token_path, tab_name="Data"):
    """
    Run a processing pass over a list of leads and write results to sheet.

    mode:
      "dm"     — call AnyMail DM endpoint (uses lead["dm_categories"])
      "person" — call AnyMail person endpoint (uses lead["full_name"])
    """
    total = len(leads)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    found_email = 0
    found_no_email = 0
    not_found = 0

    print(f"\n{'='*60}")
    print(f"{pass_name}: {total} leads ({total_batches} batches, {MAX_WORKERS} workers)")
    print(f"{'='*60}")

    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch = leads[batch_start:batch_start + BATCH_SIZE]
        batch_updates = []

        row_range = f"rows {batch[0]['row_num']}-{batch[-1]['row_num']}"
        print(f"\n--- Batch {batch_num + 1}/{total_batches} ({row_range}) ---")

        def process_one(lead):
            if mode == "dm":
                return find_dm(api_key, lead["company_url"], lead["company_name"], lead["dm_categories"])
            else:
                return find_email_person(api_key, lead["full_name"], lead["company_url"], lead["company_name"])

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_lead = {executor.submit(process_one, lead): lead for lead in batch}
            for future in as_completed(future_to_lead):
                lead = future_to_lead[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"email": None, "status": f"exception: {e}"}

                row_updates = {}

                if mode == "dm":
                    if result.get("person_name"):
                        row_updates[idx_person] = result["person_name"]
                        if idx_result_title is not None and result.get("person_title"):
                            row_updates[idx_result_title] = result["person_title"]
                        if idx_linkedin is not None and result.get("person_linkedin"):
                            row_updates[idx_linkedin] = result["person_linkedin"]
                    if result.get("email"):
                        row_updates[idx_email] = result["email"]
                    if idx_dm_confidence is not None:
                        existing_conf = lead.get("dm_confidence", "")
                        row_updates[idx_dm_confidence] = existing_conf + "_retry" if existing_conf else "retry"
                    if idx_dm_reasoning is not None and result.get("person_name"):
                        row_updates[idx_dm_reasoning] = f"Retry ({','.join(lead['dm_categories'])}): {result.get('person_name', '')} ({result.get('person_title', '')})"

                    if result.get("person_name") and result.get("email"):
                        found_email += 1
                        print(f"  Row {lead['row_num']}: {result['person_name']} <{result['email']}> ({result.get('person_title','')[:40]})")
                    elif result.get("person_name"):
                        found_no_email += 1
                        print(f"  Row {lead['row_num']}: {result['person_name']} (no email) ({result.get('person_title','')[:40]})")
                    else:
                        not_found += 1
                        print(f"  Row {lead['row_num']}: No match — {lead.get('company_name', '')} ({result['status']})")

                else:  # person mode
                    if result.get("email"):
                        row_updates[idx_email] = result["email"]
                        found_email += 1
                        print(f"  Row {lead['row_num']}: {lead['full_name']} <{result['email']}>")
                    else:
                        not_found += 1
                        print(f"  Row {lead['row_num']}: {lead['full_name']} — no email ({result['status']})")

                if row_updates:
                    for col_index, value in row_updates.items():
                        batch_updates.append({
                            "range": f"'{tab_name}'!{col_letter(col_index)}{lead['row_num']}",
                            "values": [[value]],
                        })

        write_batch_to_sheet(service, sheet_id, batch_updates, token_path, batch_num + 1)
        print(f"  → Written. Running: {found_email} with email, {found_no_email} name only, {not_found} not found")

        # Rate limit: 1.5s between sheet batch writes
        if batch_num + 1 < total_batches:
            time.sleep(SHEET_WRITE_DELAY)

    print(f"\n{pass_name} complete: {found_email} with email, {found_no_email} name only, {not_found} not found")
    return found_email, found_no_email, not_found


def main():
    parser = argparse.ArgumentParser(description="Find decision makers + emails via AnyMail Finder")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process in Pass 1 (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview rules output without calling AnyMail")
    parser.add_argument("--no_retry", action="store_true", help="Skip Pass 2 retry (for debugging)")
    args = parser.parse_args()

    api_key = os.getenv("ANYMAILFINDER_API_KEY")
    if not api_key and not args.dry_run:
        print("Error: ANYMAILFINDER_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Detect tab — try "Data" first (standard pipeline), fall back to "Leads" (Apify import)
    tab_name = "Data"
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if "Data" not in tab_titles and "Leads" in tab_titles:
        tab_name = "Leads"
    print(f"  Using tab: '{tab_name}'")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab_name
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    # Column aliases — maps canonical name → list of alternates to try
    COLUMN_ALIASES = {
        "person_name":           ["person_name", "DM Name"],
        "result_title":          ["result_title", "DM Title"],
        "linkedin_url":          ["linkedin_url", "LinkedIn URL"],
        "email":                 ["email", "Email"],
        "company name":          ["company name", "Company Name"],
        "job_title":             ["job_title", "Job Title"],
        "company_url":           ["company_url", "Company Website"],
        "company_employee_count":["company_employee_count", "Company Size"],
        "dm_confidence":         ["dm_confidence"],
        "dm_reasoning":          ["dm_reasoning"],
    }

    def col_idx(canonical):
        for alias in COLUMN_ALIASES.get(canonical, [canonical]):
            try:
                return headers.index(alias)
            except ValueError:
                continue
        return None

    idx_person = col_idx("person_name")
    idx_result_title = col_idx("result_title")
    idx_linkedin = col_idx("linkedin_url")
    idx_email = col_idx("email")
    idx_company_name = col_idx("company name")
    idx_job_title = col_idx("job_title")
    idx_company_url = col_idx("company_url")
    idx_employee_count = col_idx("company_employee_count")
    idx_dm_confidence = col_idx("dm_confidence")
    idx_dm_reasoning = col_idx("dm_reasoning")

    missing = []
    for name, idx in [
        ("person_name", idx_person), ("email", idx_email),
        ("company name", idx_company_name), ("job_title", idx_job_title),
    ]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        sys.exit(1)

    # Build lead list for Pass 1 (no person_name AND no email)
    pass1_leads = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        if cell(idx_person) or cell(idx_email):
            continue

        company_name = cell(idx_company_name)
        company_url = cell(idx_company_url)
        job_title = cell(idx_job_title)

        if not company_name or not job_title:
            continue

        # Skip if company_url is a LinkedIn URL (no domain for AMF)
        if company_url and "linkedin.com" in company_url:
            company_url = ""

        if not company_url and not company_name:
            continue

        categories, _, _ = determine_dm_category(job_title, cell(idx_employee_count))
        pass1_leads.append({
            "row_num": i,
            "company_name": company_name,
            "company_url": company_url,
            "job_title": job_title,
            "employee_count": cell(idx_employee_count),
            "dm_categories": categories,
        })

        if args.limit and len(pass1_leads) >= args.limit:
            break

    if not pass1_leads:
        print("No leads need DM lookup (all rows already have person_name or email)")
    else:
        print(f"\nFound {len(pass1_leads)} leads for Pass 1")

    # Dry run: just show rules output
    if args.dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — Rules output (no AnyMail calls)\n")
        for lead in pass1_leads[:20]:
            categories, confidence, reasoning = determine_dm_category(
                lead["job_title"], lead["employee_count"]
            )
            print(f"  Row {lead['row_num']}: {lead['company_name']}")
            print(f"    Hiring: {lead['job_title']} | Employees: {lead['employee_count'] or '?'}")
            print(f"    AMF category: {categories} | Confidence: {confidence}")
            print(f"    {reasoning}")
            print()
        if len(pass1_leads) > 20:
            print(f"  ... and {len(pass1_leads) - 20} more")
        print(f"{'='*70}")
        sys.exit(0)

    # ── PASS 1 ──────────────────────────────────────────────────────────────
    p1_email, p1_no_email, p1_not_found = 0, 0, 0
    if pass1_leads:
        p1_email, p1_no_email, p1_not_found = run_pass(
            api_key, pass1_leads, "Pass 1 (DM lookup)", "dm",
            service, sheet_id,
            idx_person, idx_result_title, idx_linkedin, idx_email,
            idx_dm_confidence, idx_dm_reasoning, token_path, tab_name,
        )

    if args.no_retry:
        print("\n--no_retry set, skipping Pass 2.")
        print_summary(p1_email, p1_no_email, p1_not_found, 0, 0, 0)
        sys.exit(0)

    # ── PASS 2: Re-read sheet to find retry candidates ───────────────────────
    print("\nRe-reading sheet for Pass 2 retry candidates...")
    result2 = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab_name
    ).execute()
    all_rows2 = result2.get("values", [])

    person_retry = []   # Has person_name but no email → try /person endpoint
    dm_retry = []       # No person_name at all → flip DM category

    for i, row in enumerate(all_rows2[1:], start=2):
        def cell2(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        person_name = cell2(idx_person)
        email = cell2(idx_email)
        company_name = cell2(idx_company_name)
        company_url = cell2(idx_company_url)
        job_title = cell2(idx_job_title)

        if email:
            continue  # Already has email, skip

        if company_url and "linkedin.com" in company_url:
            company_url = ""

        if not company_url and not company_name:
            continue

        if person_name:
            # Has name, needs email
            person_retry.append({
                "row_num": i,
                "full_name": person_name,
                "company_url": company_url,
                "company_name": company_name,
            })
        else:
            # No name found at all — flip category
            if not job_title or not company_name:
                continue
            categories, _, _ = determine_dm_category(job_title, cell2(idx_employee_count))
            flipped = flip_category(categories)
            dm_retry.append({
                "row_num": i,
                "company_name": company_name,
                "company_url": company_url,
                "job_title": job_title,
                "employee_count": cell2(idx_employee_count),
                "dm_categories": flipped,
                "dm_confidence": cell2(idx_dm_confidence),
            })

    print(f"  Pass 2 candidates: {len(person_retry)} with name (no email), {len(dm_retry)} with no name (flip category)")

    # ── PASS 2a: Find email for known people ─────────────────────────────────
    p2a_email, p2a_no_email, p2a_not_found = 0, 0, 0
    if person_retry:
        p2a_email, p2a_no_email, p2a_not_found = run_pass(
            api_key, person_retry, "Pass 2a (person email lookup)", "person",
            service, sheet_id,
            idx_person, idx_result_title, idx_linkedin, idx_email,
            idx_dm_confidence, idx_dm_reasoning, token_path, tab_name,
        )

    # ── PASS 2b: Flip DM category for no-match leads ────────────────────────
    p2b_email, p2b_no_email, p2b_not_found = 0, 0, 0
    if dm_retry:
        p2b_email, p2b_no_email, p2b_not_found = run_pass(
            api_key, dm_retry, "Pass 2b (flipped DM category)", "dm",
            service, sheet_id,
            idx_person, idx_result_title, idx_linkedin, idx_email,
            idx_dm_confidence, idx_dm_reasoning, token_path, tab_name,
        )

    print_summary(p1_email, p1_no_email, p1_not_found,
                  p2a_email + p2b_email, p2a_no_email + p2b_no_email, p2a_not_found + p2b_not_found)


def print_summary(p1_email, p1_no_email, p1_not_found, p2_email, p2_no_email, p2_not_found):
    total_email = p1_email + p2_email
    total_no_email = p1_no_email + p2_no_email
    total_not_found = p1_not_found + p2_not_found
    print(f"\n{'='*60}")
    print(f"Find DM Complete")
    print(f"  Pass 1 — with email: {p1_email}, name only: {p1_no_email}, not found: {p1_not_found}")
    print(f"  Pass 2 — with email: {p2_email}, name only: {p2_no_email}, not found: {p2_not_found}")
    print(f"  Total  — with email: {total_email}, name only: {total_no_email}, not found: {total_not_found}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
