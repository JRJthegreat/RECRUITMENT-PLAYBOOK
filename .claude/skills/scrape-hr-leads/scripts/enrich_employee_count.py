"""
Enrich real employee counts from LinkedIn company profiles.

Uses Apify actor dev_fusion/Linkedin-Company-Scraper to scrape LinkedIn
company pages and extract employee count.

Reads LinkedIn URLs from column AA, writes employee count to column H.

Run:
  python3 -W ignore enrich_employee_count.py --sheet_url "URL" --tab "TAB"
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
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "dev_fusion~Linkedin-Company-Scraper"
APIFY_BASE = "https://api.apify.com/v2"

BATCH = 10
APIFY_BATCH_SIZE = 25  # companies per Apify call
SHEET_WRITE_DELAY = 2


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


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def extract_employee_count(company_data):
    """Extract employee count from LinkedIn company scraper result.

    Schema returns:
    - employeeCount: exact int (e.g. 83855)
    - employeeCountRange: {"start": 10001, "end": null}
    """
    # Exact count
    count = company_data.get("employeeCount")
    if isinstance(count, int) and count > 0:
        return count

    # Range fallback (take upper bound, or start if no end)
    range_data = company_data.get("employeeCountRange")
    if isinstance(range_data, dict):
        end = range_data.get("end")
        start = range_data.get("start")
        if isinstance(end, int) and end > 0:
            return end
        if isinstance(start, int) and start > 0:
            return start

    return None


def scrape_linkedin_companies(linkedin_urls):
    """Scrape LinkedIn company profiles via Apify in batches.
    Returns dict: normalized_slug -> company data.
    """
    print(f"  Scraping {len(linkedin_urls)} LinkedIn company profiles (batches of {APIFY_BATCH_SIZE})...")
    all_results = {}

    for batch_start in range(0, len(linkedin_urls), APIFY_BATCH_SIZE):
        batch = linkedin_urls[batch_start:batch_start + APIFY_BATCH_SIZE]
        batch_num = batch_start // APIFY_BATCH_SIZE + 1
        total_batches = (len(linkedin_urls) + APIFY_BATCH_SIZE - 1) // APIFY_BATCH_SIZE
        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} companies)...")

        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json={"profileUrls": batch},
                timeout=300,
            )
        except requests.exceptions.Timeout:
            print(f"  Timeout on batch {batch_num}, skipping...")
            continue

        if resp.status_code not in (200, 201):
            print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:500]}")
            continue

        items = resp.json()
        print(f"  Got {len(items)} results")

        for item in items:
            item_url = item.get("url", "")
            # Normalize: extract slug from URL for matching
            slug = re.sub(r"https?://(www\.)?linkedin\.com/company/", "", item_url).rstrip("/").lower()
            all_results[slug] = item

    print(f"\n  Total scraped: {len(all_results)} company profiles")
    return all_results


def flush_updates(service, updates, sheet_id, tab, col_idx):
    if not updates:
        return
    data = [
        {"range": f"'{tab}'!{col_letter(col_idx)}{u['sheet_row']}",
         "values": [[u["value"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"\n  -> Wrote {len(updates)} employee counts to sheet")
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Enrich real employee counts from LinkedIn")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL or ID")
    parser.add_argument("--tab", required=True, help="Tab name")
    parser.add_argument("--col_company_name", type=int, default=0, help="0-indexed column for company name")
    parser.add_argument("--col_employee_count", type=int, default=7, help="0-indexed column for employee count (default: H=7)")
    parser.add_argument("--col_linkedin", type=int, default=26, help="0-indexed column for LinkedIn URL (default: AA=26)")
    args = parser.parse_args()

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab
    COL_NAME = args.col_company_name
    COL_EMPLOYEE = args.col_employee_count
    COL_LINKEDIN = args.col_linkedin

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Enrich Employee Counts from LinkedIn ===\n")
    service = get_google_service()

    # Read sheet
    print("[1/3] Reading sheet...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AZ"
    ).execute()
    data = result.get("values", [])[1:]
    print(f"  {len(data)} total rows")

    # Find rows with LinkedIn URL
    targets = []
    for i, row in enumerate(data):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        if name.strip() and linkedin.strip():
            targets.append({
                "sheet_row": i + 2,
                "company": name.strip(),
                "linkedin": linkedin.strip(),
            })

    print(f"  {len(targets)} companies with LinkedIn URL to enrich\n")
    if not targets:
        print("Nothing to enrich.")
        return

    # Scrape LinkedIn company profiles
    print(f"[2/3] Scraping LinkedIn company profiles...")
    linkedin_urls = [t["linkedin"] for t in targets]
    scraped = scrape_linkedin_companies(linkedin_urls)

    # Match results and extract employee counts
    print("\n  Extracting employee counts...")
    updates = []
    found = not_found = 0

    for target in targets:
        company = target["company"]
        linkedin = target["linkedin"].rstrip("/")

        # Match by normalized slug
        slug = re.sub(r"https?://(www\.)?linkedin\.com/company/", "", linkedin).rstrip("/").lower()
        company_data = scraped.get(slug, {})

        count = extract_employee_count(company_data) if company_data else None

        if count is not None:
            found += 1
            updates.append({"sheet_row": target["sheet_row"], "value": count})
            print(f"  +  {company[:50]:50s} -> {count:,} employees")
        else:
            not_found += 1
            print(f"  x  {company[:50]:50s} -> (not found)")

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB, COL_EMPLOYEE)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB, COL_EMPLOYEE)

    print(f"\n[3/3] Summary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()
