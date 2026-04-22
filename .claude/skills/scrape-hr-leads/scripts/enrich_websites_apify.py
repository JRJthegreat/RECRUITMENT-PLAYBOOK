"""
Fallback website enrichment using Apify Google Search Scraper.

Reads the HR Leads - Apify Final sheet, finds rows that STILL have a
company name but no website (i.e. domain guessing failed), batches them
into a single Apify Google Search run, then verifies and writes results
back to column L.

Run AFTER enrich_websites.py:
  python3 -W ignore enrich_websites_apify.py
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
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

BATCH = 10

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_DOMAINS = {
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "yelp.com", "bloomberg.com", "crunchbase.com",
    "zoominfo.com", "wikipedia.org", "pitchbook.com", "dnb.com",
    "bizapedia.com", "bbb.org", "rocketreach.co", "apollo.io",
    "signalhire.com", "clearbit.com", "owler.com", "manta.com",
}


# ── Verification ─────────────────────────────────────────────────────────────

def _name_words(company_name):
    noise = {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&",
             "a", "an", "for", "in", "at", "by"}
    words = re.split(r"[\s,.\-&/()+]+", company_name.lower())
    return [w for w in words if len(w) > 2 and w not in noise]


def verify(url, company_name):
    """Fetch URL and confirm it belongs to the company."""
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=8, allow_redirects=True)
        if r.status_code == 200:
            content = r.text[:30000].lower()
            words = _name_words(company_name)
            if not words:
                return True
            matches = sum(1 for w in words if w in content)
            return matches >= max(1, len(words) // 2)
        elif r.status_code in (403, 503):
            # Cloudflare — check domain name instead
            domain = re.sub(r"^https?://(www\.)?", "", r.url).split("/")[0].lower()
            words = _name_words(company_name)
            if not words:
                return True
            return sum(1 for w in words if w in domain) >= max(1, len(words) // 2)
    except Exception:
        pass
    return False


# ── Google Sheets ─────────────────────────────────────────────────────────────

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


def flush_updates(service, updates, sheet_id, tab, col_website):
    if not updates:
        return
    data = [
        {"range": f"'{tab}'!{col_letter(col_website)}{u['sheet_row']}",
         "values": [[u["website"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"  → Wrote {len(updates)} websites to sheet")


# ── Apify Google Search ───────────────────────────────────────────────────────

APIFY_BATCH_SIZE = 50  # queries per Apify call to avoid timeouts


def apify_google_search(queries):
    """
    Run Apify Google Search Scraper for a list of queries.
    Batches into chunks of APIFY_BATCH_SIZE to avoid timeouts.
    Returns dict: query → list of result URLs (organic only).
    """
    print(f"  Sending {len(queries)} queries to Apify Google Search Scraper (batches of {APIFY_BATCH_SIZE})...")

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
            continue

        items = resp.json()
        for item in items:
            query = item.get("searchQuery", {}).get("term", "")
            organic = item.get("organicResults", [])
            urls = []
            for r in organic:
                url = r.get("url", "")
                if url:
                    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
                    if not any(skip in domain for skip in SKIP_DOMAINS):
                        urls.append(url)
            if query and urls:
                all_results[query] = urls

        print(f"  Batch {batch_num} done — {len(all_results)} total results so far")

    print(f"\n  Got results for {len(all_results)}/{len(queries)} queries total")
    return all_results


# ── Main ──────────────────────────────────────────────────────────────────────

def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def main():
    parser = argparse.ArgumentParser(description="Enrich missing company websites via Apify Google Search")
    parser.add_argument("--sheet_url", default="1jopIsvbAmhxoQmmKXAQTBp1zeujzwOfNAPBsNaCpWqA", help="Google Sheet URL or ID")
    parser.add_argument("--tab", default="Leads", help="Tab name")
    parser.add_argument("--col_company_name", type=int, default=10, help="0-indexed column for company name")
    parser.add_argument("--col_company_url", type=int, default=11, help="0-indexed column for company URL/website")
    args = parser.parse_args()

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab
    COL_COMPANY_NAME = args.col_company_name
    COL_COMPANY_WEBSITE = args.col_company_url

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    print("=== Enrich Websites (Apify Google Search) ===\n")
    service = get_google_service()

    # Read sheet — find rows still missing a website
    print("[1/3] Reading sheet for remaining gaps...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AZ"
    ).execute()
    data = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(data):
        company = row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else ""
        website = row[COL_COMPANY_WEBSITE] if len(row) > COL_COMPANY_WEBSITE else ""
        if company.strip() and not website.strip():
            targets.append({"sheet_row": i + 2, "company": company.strip()})

    print(f"  {len(targets)} companies still missing a website")
    if not targets:
        print("  Nothing to enrich — all companies already have websites!")
        return

    # Batch all queries into one Apify run
    print(f"\n[2/3] Running Apify Google Search for {len(targets)} companies...")
    queries = [f"{t['company']} official website" for t in targets]
    # Build lookup: query → target info
    query_map = {f"{t['company']} official website": t for t in targets}

    search_results = apify_google_search(queries)

    # Match results back, verify, collect updates
    print("\n  Verifying results...")
    updates = []
    found = not_found = 0

    for query, target in query_map.items():
        company = target["company"]
        urls = search_results.get(query, [])
        verified_url = ""

        for url in urls:
            if verify(url, company):
                verified_url = url.split("?")[0].rstrip("/")
                break

        status = "✓" if verified_url else "✗"
        print(f"  {status}  {company[:50]:50s} → {verified_url or '(not found)'}")

        if verified_url:
            found += 1
            updates.append({"sheet_row": target["sheet_row"], "website": verified_url})
        else:
            not_found += 1

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB, COL_COMPANY_WEBSITE)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB, COL_COMPANY_WEBSITE)

    print(f"\n[3/3] Summary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()
