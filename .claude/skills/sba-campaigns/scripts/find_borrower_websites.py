"""
Phase 1: Find correct websites for SBA rural borrowers.

Searches Google for "{company} {city}, {state} official website" then
validates the result — the domain MUST contain at least one meaningful
word from the company name. If it doesn't, it's rejected (better nothing
than a wrong website that poisons downstream enrichment).

Reads:  col A=borrower_name, col B=city, col C=state
Writes: col M=website

Run:
  python3 -W ignore find_borrower_websites.py [--limit N]
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"

SHEET_ID = "1WgIhmQmJ1XhYHIVb6DgPuvBG1ex1_k76fPvr9BBVfR0"
TAB = "dataset_sba-rural-loans_2026-04-16_05-40-32-227"
COL_WEBSITE = 12  # M

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 1

SKIP_DOMAINS = {
    "indeed.com", "linkedin.com", "glassdoor.com", "facebook.com",
    "twitter.com", "instagram.com", "youtube.com", "yelp.com",
    "bloomberg.com", "crunchbase.com", "zoominfo.com", "wikipedia.org",
    "dnb.com", "bizapedia.com", "bbb.org", "rocketreach.co", "apollo.io",
    "manta.com", "mapquest.com", "yellowpages.com", "chamberofcommerce.com",
    "opencorporates.com", "buzzfile.com", "dandb.com",
    # Industry SaaS / directories that caused bad results last time
    "roserocket.com", "bubba.ai", "constructconnect.com", "thebluebook.com",
    "procore.com", "gaf.com", "asbtdc.org", "aksbdc.org", "bankrupt.com",
    "npiscan.com", "pacermonitor.com", "searchcarriers.com", "lanefinder.com",
    "betterworld.org", "porch.com", "bluebookservices.com", "2moda.com",
    "tradingcomputers.com", "contratados.org", "state.gov", "dccouncil.gov",
    "smartgirlstories.com", "mdpi.com", "vailvalleypartnership.com",
    "claytonhomes.com", "husqvarna.com", "husqvarnagroup.com",
    "benzshops.com", "bimmershops.com",
    # Generic
    "sba.gov", "usda.gov", "grants.gov", "sec.gov",
    "city-data.com", "local.yahoo.com", "msn.com",
}

NOISE_WORDS = {
    "llc", "inc", "corp", "ltd", "co", "the", "of", "and", "a", "an",
    "for", "in", "at", "by", "on", "to", "or", "pllc", "dba", "services",
    "service", "group", "company", "enterprises", "solutions", "systems",
    "professionals", "associates", "partners", "international", "global",
    "national", "american", "usa", "us",
}


def company_words(name):
    """Extract meaningful words from company name for domain matching."""
    words = re.split(r"[\s,.\-&/()+]+", name.lower())
    return [w for w in words if len(w) >= 3 and w not in NOISE_WORDS]


def domain_matches_company(domain, company_name):
    """Check if domain contains at least one core word from company name."""
    domain_clean = domain.lower().replace("-", "").replace(".", "")
    for w in company_words(company_name):
        if w in domain_clean:
            return True
    return False


def is_skip_domain(url):
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    return any(s in domain for s in SKIP_DOMAINS)


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def get_service():
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


def flush_updates(service, updates):
    if not updates:
        return
    data = [
        {"range": f"'{TAB}'!{col_letter(COL_WEBSITE)}{u['row']}", "values": [[u["url"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} websites", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    service = get_service()
    print("=== Find Borrower Websites (strict domain validation) ===\n", flush=True)

    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:M"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[0] if len(row) > 0 else ""
        city = row[1] if len(row) > 1 else ""
        state = row[2] if len(row) > 2 else ""
        website = row[12] if len(row) > 12 else ""
        if name.strip() and not website.strip():
            targets.append({"row": i+2, "name": name.strip(), "city": city.strip(), "state": state.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need website\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Searching Google ({len(targets)} queries, batches of {APIFY_BATCH_SIZE})...", flush=True)
    queries = []
    qmap = {}
    for t in targets:
        loc = f"{t['city']}, {t['state']}" if t['city'] and t['state'] else t['state']
        q = f'"{t["name"]}" {loc} official website'.strip()
        queries.append(q)
        qmap[q] = t

    all_results = {}
    for bs in range(0, len(queries), APIFY_BATCH_SIZE):
        batch = queries[bs:bs+APIFY_BATCH_SIZE]
        bn = bs//APIFY_BATCH_SIZE+1; tb = (len(queries)+APIFY_BATCH_SIZE-1)//APIFY_BATCH_SIZE
        print(f"  Batch {bn}/{tb} ({len(batch)} queries)...", flush=True)
        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/apify~google-search-scraper/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json={"queries": "\n".join(batch), "resultsPerPage": 5,
                      "maxPagesPerQuery": 1, "languageCode": "en",
                      "countryCode": "us", "includeUnfilteredResults": False},
                timeout=300)
        except requests.exceptions.Timeout:
            print(f"  Timeout batch {bn}, skipping...", flush=True); continue
        if resp.status_code not in (200, 201):
            print(f"  ERROR {resp.status_code}: {resp.text[:200]}", flush=True); continue
        for item in resp.json():
            q = item.get("searchQuery", {}).get("term", "")
            organic = item.get("organicResults", [])
            urls = [r.get("url", "") for r in organic if r.get("url")]
            if q and urls:
                all_results[q] = urls
        print(f"  Batch {bn} done — {len(all_results)} results", flush=True)

    print(f"\n[3/3] Validating and writing...", flush=True)
    updates = []
    found = rejected = noresult = 0

    for q, t in qmap.items():
        urls = all_results.get(q, [])
        chosen = None
        for url in urls:
            if is_skip_domain(url):
                continue
            domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
            if domain_matches_company(domain, t["name"]):
                chosen = url.split("?")[0].rstrip("/")
                break

        if chosen:
            found += 1
            updates.append({"row": t["row"], "url": chosen})
            print(f"  +  {t['name'][:45]:45s} -> {chosen[:50]}", flush=True)
        elif not urls:
            noresult += 1
        else:
            rejected += 1
            print(f"  x  {t['name'][:45]:45s} -> (no domain match — skipped)", flush=True)

        if len(updates) >= BATCH:
            flush_updates(service, updates)
            updates = []

    if updates:
        flush_updates(service, updates)

    print(f"\nSummary: Found {found}, Rejected {rejected} (no domain match), No results {noresult} / {len(targets)}", flush=True)


if __name__ == "__main__":
    main()
