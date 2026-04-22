"""
Find LinkedIn company URLs via Apify Google Search.

For each company with a website but no LinkedIn URL, searches Google for
"{company_name} site:linkedin.com/company" and extracts the LinkedIn URL.

Writes results to a specified column in batches of 10.

Run:
  python3 -W ignore find_linkedin_urls.py --sheet_url "URL" --tab "TAB" \
    --col_company_name 0 --col_linkedin 26
"""

import os
import re
import json
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
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

import time

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 2  # seconds between writes to avoid rate limits


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


def extract_linkedin_url(urls):
    """Extract the best linkedin.com/company URL from search results."""
    for url in urls:
        # Match linkedin.com/company/slug patterns
        match = re.match(r"https?://(www\.)?linkedin\.com/company/[^/?#]+", url)
        if match:
            return match.group(0)
    return ""


def apify_google_search(queries):
    """Run Apify Google Search in batches. Returns dict: query -> list of URLs."""
    print(f"  Sending {len(queries)} queries (batches of {APIFY_BATCH_SIZE})...")
    all_results = {}

    for batch_start in range(0, len(queries), APIFY_BATCH_SIZE):
        batch = queries[batch_start:batch_start + APIFY_BATCH_SIZE]
        batch_num = batch_start // APIFY_BATCH_SIZE + 1
        total_batches = (len(queries) + APIFY_BATCH_SIZE - 1) // APIFY_BATCH_SIZE
        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} queries)...")

        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "queries": "\n".join(batch),
                "resultsPerPage": 3,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "countryCode": "us",
                "includeUnfilteredResults": False,
            },
            timeout=300,
        )

        if resp.status_code not in (200, 201):
            print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:300]}")
            continue

        items = resp.json()
        for item in items:
            query = item.get("searchQuery", {}).get("term", "")
            organic = item.get("organicResults", [])
            urls = [r.get("url", "") for r in organic if r.get("url")]
            if query and urls:
                all_results[query] = urls

        print(f"  Batch {batch_num} done — {len(all_results)} results so far")

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
    print(f"\n  -> Wrote {len(updates)} LinkedIn URLs to sheet")
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Find LinkedIn company URLs via Google Search")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL or ID")
    parser.add_argument("--tab", required=True, help="Tab name")
    parser.add_argument("--col_company_name", type=int, default=0, help="0-indexed column for company name")
    parser.add_argument("--col_linkedin", type=int, default=26, help="0-indexed column to write LinkedIn URL (default: AA=26)")
    args = parser.parse_args()

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab
    COL_NAME = args.col_company_name
    COL_LINKEDIN = args.col_linkedin

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Find LinkedIn Company URLs ===\n")
    service = get_google_service()

    # Write header
    header_cell = f"'{TAB}'!{col_letter(COL_LINKEDIN)}1"
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=header_cell,
        valueInputOption="RAW", body={"values": [["company_linkedin_url"]]}
    ).execute()

    # Read sheet
    print("[1/3] Reading sheet...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AZ"
    ).execute()
    data = result.get("values", [])[1:]
    print(f"  {len(data)} total rows")

    targets = []
    for i, row in enumerate(data):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        if name.strip() and not linkedin.strip():
            targets.append({"sheet_row": i + 2, "company": name.strip()})

    print(f"  {len(targets)} companies need LinkedIn URL\n")
    if not targets:
        print("Nothing to enrich.")
        return

    # Search Google for LinkedIn URLs
    print(f"[2/3] Searching Google for LinkedIn company pages...")
    queries = [f"{t['company']} site:linkedin.com/company" for t in targets]
    query_map = {f"{t['company']} site:linkedin.com/company": t for t in targets}

    search_results = apify_google_search(queries)

    # Extract LinkedIn URLs from results
    print("\n  Extracting LinkedIn URLs...")
    updates = []
    found = not_found = 0

    for query, target in query_map.items():
        company = target["company"]
        urls = search_results.get(query, [])
        linkedin_url = extract_linkedin_url(urls)

        status = "+" if linkedin_url else "x"
        print(f"  {status}  {company[:50]:50s} -> {linkedin_url or '(not found)'}")

        if linkedin_url:
            found += 1
            updates.append({"sheet_row": target["sheet_row"], "value": linkedin_url})
        else:
            not_found += 1

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB, COL_LINKEDIN)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB, COL_LINKEDIN)

    print(f"\n[3/3] Summary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()
