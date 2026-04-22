"""
Phase 3: Find decision makers for USDA lenders via Google Search + LinkedIn.

Size-based targeting:
  - <200 employees:   CEO / President / Owner / Founder
  - 200-1000:         VP Lending / CLO / SVP Lending / Chief Lending Officer
  - 1000+:            Director of Lending / Head of Commercial Lending

For each lender without a DM:
1. Determine target title based on employee count (column H)
2. Search Google: "{lender_name}" {target titles} site:linkedin.com/in/
3. Parse LinkedIn snippet for person name + title
4. Validate title matches target category
5. Write dm_name, dm_title, dm_linkedin to sheet

Auto-retry: flips category for rows where no match was found
(e.g. if VP Lending not found at mid-size bank, try CEO).

Run:
  python3 -W ignore find_dm_lenders.py --sheet_url "URL" --tab "TAB"
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

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 2


# --- Targeting rules ---

def determine_target(employee_count):
    """Determine DM target level based on employee count."""
    if employee_count is None or employee_count < 200:
        return "ceo", f"Small ({employee_count or '?'} employees), targeting CEO/President"
    if employee_count <= 1000:
        return "vp_lending", f"Mid-size ({employee_count} employees), targeting VP Lending/CLO"
    return "director_lending", f"Large ({employee_count} employees), targeting Director of Lending"


def flip_target(target):
    if target == "ceo":
        return "vp_lending"
    return "ceo"


def build_search_query(company_name, target_level):
    if target_level == "ceo":
        return f'"{company_name}" ("CEO" OR "President" OR "Owner" OR "Founder" OR "Managing Director") site:linkedin.com/in/'
    elif target_level == "vp_lending":
        return f'"{company_name}" ("VP" OR "SVP" OR "Vice President" OR "Chief" OR "Head") ("Lending" OR "Loan" OR "Credit" OR "Banking") site:linkedin.com/in/'
    elif target_level == "director_lending":
        return f'"{company_name}" ("Director" OR "Head" OR "Senior Vice President") ("Lending" OR "Loan" OR "Credit" OR "Commercial" OR "Banking") site:linkedin.com/in/'
    return f'"{company_name}" "CEO" site:linkedin.com/in/'


# --- LinkedIn result parsing ---

def parse_linkedin_result(organic_result):
    """Extract person name and title from a LinkedIn search result."""
    title = organic_result.get("title", "")
    url = organic_result.get("url", "")

    if "linkedin.com/in/" not in url:
        return None, None, None

    # Clean title: remove " | LinkedIn" or " - LinkedIn" suffix
    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()

    # Format: "Name - Title - Company" or "Name – Title at Company"
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    if len(parts) >= 2:
        person_name = parts[0].strip()
        result_title = parts[1].strip()
        result_title = re.sub(r"\s+at\s+.*$", "", result_title, flags=re.IGNORECASE).strip()
        return person_name, result_title, url

    # Format: "Name, Title"
    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url

    return None, None, url


def validate_result(person_name, result_title, target_level):
    """Validate that the search result matches our target category."""
    if not person_name or not result_title:
        return False

    title_lower = result_title.lower()

    if target_level == "ceo":
        keywords = ["ceo", "president", "owner", "founder", "managing director",
                     "co-founder", "cofounder", "chief executive"]
        return any(kw in title_lower for kw in keywords)

    elif target_level == "vp_lending":
        level_kw = ["vp", "vice president", "svp", "senior vice president",
                     "chief", "head of", "evp", "executive vice president"]
        domain_kw = ["lending", "loan", "credit", "banking", "commercial",
                     "mortgage", "finance", "financial"]
        has_level = any(kw in title_lower for kw in level_kw)
        has_domain = any(kw in title_lower for kw in domain_kw)
        return has_level and has_domain

    elif target_level == "director_lending":
        level_kw = ["director", "head", "senior vice president", "svp", "managing director"]
        domain_kw = ["lending", "loan", "credit", "commercial", "banking",
                     "mortgage", "finance", "financial"]
        has_level = any(kw in title_lower for kw in level_kw)
        has_domain = any(kw in title_lower for kw in domain_kw)
        return has_level and has_domain

    return False


def parse_employee_count(val):
    """Parse employee count from string or int."""
    if isinstance(val, int):
        return val
    if not val:
        return None
    s = str(val).strip().replace(",", "").replace("+", "")
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


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


# --- Apify Google Search ---

def apify_google_search(queries):
    """Run Apify Google Search in batches. Returns dict: query -> organic results."""
    all_results = {}

    for batch_start in range(0, len(queries), APIFY_BATCH_SIZE):
        batch = queries[batch_start:batch_start + APIFY_BATCH_SIZE]
        batch_num = batch_start // APIFY_BATCH_SIZE + 1
        total_batches = (len(queries) + APIFY_BATCH_SIZE - 1) // APIFY_BATCH_SIZE
        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} queries)...")

        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json={
                    "queries": "\n".join(batch),
                    "resultsPerPage": 5,
                    "maxPagesPerQuery": 1,
                    "languageCode": "en",
                    "countryCode": "us",
                    "includeUnfilteredResults": False,
                },
                timeout=300,
            )
        except requests.exceptions.Timeout:
            print(f"  Timeout on batch {batch_num}, skipping...")
            continue

        if resp.status_code not in (200, 201):
            print(f"  ERROR: HTTP {resp.status_code}: {resp.text[:300]}")
            continue

        items = resp.json()
        for item in items:
            query = item.get("searchQuery", {}).get("term", "")
            organic = item.get("organicResults", [])
            if query and organic:
                all_results[query] = organic

        print(f"  Batch {batch_num} done — {len(all_results)} results so far")

    return all_results


def flush_updates(service, updates, sheet_id, tab):
    """Write dm_name, dm_title, dm_linkedin to sheet."""
    if not updates:
        return
    data = []
    for u in updates:
        for col_idx, value in u["cells"].items():
            data.append({
                "range": f"'{tab}'!{col_letter(col_idx)}{u['sheet_row']}",
                "values": [[value]],
            })
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"  -> Wrote {len(updates)} DM records to sheet")
    time.sleep(SHEET_WRITE_DELAY)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Find decision makers for USDA lenders")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL or ID")
    parser.add_argument("--tab", required=True, help="Tab name")
    parser.add_argument("--col_company_name", type=int, default=0, help="Column for company name (default: A=0)")
    parser.add_argument("--col_employee_count", type=int, default=7, help="Column for employee count (default: H=7)")
    parser.add_argument("--col_dm_name", type=int, default=27, help="Column for DM name (default: AB=27)")
    parser.add_argument("--col_dm_title", type=int, default=28, help="Column for DM title (default: AC=28)")
    parser.add_argument("--col_dm_linkedin", type=int, default=29, help="Column for DM LinkedIn URL (default: AD=29)")
    parser.add_argument("--retry", action="store_true", help="Retry pass: flip target for rows with no DM")
    args = parser.parse_args()

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab
    COL_NAME = args.col_company_name
    COL_EMPLOYEE = args.col_employee_count
    COL_DM_NAME = args.col_dm_name
    COL_DM_TITLE = args.col_dm_title
    COL_DM_LINKEDIN = args.col_dm_linkedin

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    mode = "RETRY (flipped targets)" if args.retry else "PASS 1"
    print(f"=== Find Decision Makers for USDA Lenders [{mode}] ===\n")
    service = get_google_service()

    # Write headers
    headers = {COL_DM_NAME: "dm_name", COL_DM_TITLE: "dm_title", COL_DM_LINKEDIN: "dm_linkedin"}
    for col_idx, header in headers.items():
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{TAB}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()

    # Read sheet
    print("[1/3] Reading sheet...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AZ"
    ).execute()
    data = result.get("values", [])[1:]
    print(f"  {len(data)} total rows")

    # Find rows needing DM
    targets = []
    for i, row in enumerate(data):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        employee = row[COL_EMPLOYEE] if len(row) > COL_EMPLOYEE else ""
        dm_name = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""

        if not name.strip():
            continue
        if dm_name.strip():
            continue  # already has DM

        emp_count = parse_employee_count(employee)
        target_level, reasoning = determine_target(emp_count)

        if args.retry:
            target_level = flip_target(target_level)
            reasoning = f"RETRY: {reasoning} -> flipped to {target_level}"

        targets.append({
            "sheet_row": i + 2,
            "company": name.strip(),
            "employee_count": emp_count,
            "target_level": target_level,
            "reasoning": reasoning,
        })

    print(f"  {len(targets)} lenders need DM lookup")
    if not targets:
        print("  Nothing to do — all lenders already have DMs!")
        return

    # Count by target level
    by_level = {}
    for t in targets:
        by_level[t["target_level"]] = by_level.get(t["target_level"], 0) + 1
    for level, count in sorted(by_level.items()):
        print(f"  {level}: {count}")

    # Build search queries
    print(f"\n[2/3] Searching Google for DMs ({len(targets)} queries)...")
    queries = [build_search_query(t["company"], t["target_level"]) for t in targets]
    query_map = {build_search_query(t["company"], t["target_level"]): t for t in targets}

    search_results = apify_google_search(queries)

    # Parse results
    print("\n  Matching DMs...")
    updates = []
    found = not_found = 0

    for query, target in query_map.items():
        company = target["company"]
        organic = search_results.get(query, [])

        best_name = None
        best_title = None
        best_url = None

        for result in organic:
            name, title, url = parse_linkedin_result(result)
            if name and title and validate_result(name, title, target["target_level"]):
                best_name = name
                best_title = title
                best_url = url
                break

        if best_name:
            found += 1
            updates.append({
                "sheet_row": target["sheet_row"],
                "cells": {
                    COL_DM_NAME: best_name,
                    COL_DM_TITLE: best_title,
                    COL_DM_LINKEDIN: best_url or "",
                },
            })
            print(f"  +  {company[:40]:40s} [{target['target_level']:16s}] -> {best_name} ({best_title})")
        else:
            not_found += 1
            print(f"  x  {company[:40]:40s} [{target['target_level']:16s}] -> (not found)")

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB)

    print(f"\n[3/3] Summary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    if not_found > 0 and not args.retry:
        print(f"\n  Tip: Run with --retry to flip targets for {not_found} remaining lenders")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()
