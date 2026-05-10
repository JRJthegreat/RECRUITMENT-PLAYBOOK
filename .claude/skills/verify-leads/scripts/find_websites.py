"""
Find verified company websites via Apify Google Search.

Strict validation: the domain MUST contain at least one BRAND word from the
company name. Generic industry words (electric, roofing, trucking, etc.) alone
are NOT sufficient — they match too many unrelated directories and SaaS sites.

Run:
  python3 -W ignore find_websites.py \
    --sheet_url "URL" --tab "TAB" \
    --col_name 0 --col_city 1 --col_state 2 --col_website 12
"""

import os
import re
import sys
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
APIFY_BASE = "https://api.apify.com/v2"

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 1

# Domains always skipped regardless of name match
SKIP_DOMAINS = {
    # Job / professional networks
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "facebook.com", "twitter.com", "instagram.com",
    # Directories / aggregators
    "yelp.com", "bloomberg.com", "crunchbase.com", "zoominfo.com",
    "wikipedia.org", "dnb.com", "bizapedia.com", "bbb.org",
    "rocketreach.co", "apollo.io", "manta.com", "mapquest.com",
    "yellowpages.com", "chamberofcommerce.com", "opencorporates.com",
    "buzzfile.com", "dandb.com", "youtube.com", "msn.com",
    "local.yahoo.com", "city-data.com",
    # Government / legal databases
    "sba.gov", "usda.gov", "grants.gov", "sec.gov", "state.gov",
    "bankrupt.com", "pacermonitor.com",
    # Industry SaaS that publish company-mentions (previously caused bad matches)
    "roserocket.com", "bubba.ai", "constructconnect.com", "thebluebook.com",
    "procore.com", "gaf.com", "porch.com", "searchcarriers.com",
    "lanefinder.com", "bluebookservices.com", "tradingcomputers.com",
    "contratados.org", "smartgirlstories.com", "mdpi.com",
    "vailvalleypartnership.com", "claytonhomes.com",
    "husqvarna.com", "husqvarnagroup.com", "benzshops.com", "bimmershops.com",
    # Small biz support orgs (SBDC, SBA programs — mention companies but aren't them)
    "asbtdc.org", "aksbdc.org", "betterworld.org", "npiscan.com",
    # Hosted website platforms (company uses subdomain, not a real domain)
    "wixsite.com", "weebly.com", "squarespace.com", "godaddysites.com",
    "site123.me", "webflow.io", "carrd.co", "strikingly.com",
    # Job application / HR platforms
    "applytojob.com", "peopleasetalent.com", "iapplicants.com",
    "workable.com", "greenhouse.io", "lever.co",
    # Restaurant / local listing platforms
    "placejoys.com", "tripadvisor.com", "grubhub.com", "doordash.com",
    "opentable.com",
    # Pawn / niche directories
    "pawnfinders.com",
    # Franchise parent domains (match company type but not the specific business)
    "anytimefitness.ca", "anytimefitness.com", "subway.com", "mcdonalds.com",
    # News / local media
    "corsicanadailysun.com",
}

# Industry/generic words that alone cannot confirm a domain belongs to this company.
# Only non-generic "brand" words trigger a domain match.
GENERIC_INDUSTRY_WORDS = {
    # Trades
    "electric", "electrical", "roofing", "roof", "roofer",
    "construction", "builder", "builders", "build",
    "trucking", "truck", "trucks", "hauling", "haul", "freight", "cargo",
    "plumbing", "plumber", "hvac", "heating", "cooling", "mechanical",
    "painting", "painter", "landscaping", "landscape", "lawn", "mowing",
    "tree", "welding", "welder", "metal", "fabrication", "fab",
    "auto", "automotive", "car", "cars", "tire", "tires", "mechanic",
    "garage", "repair", "repairs",
    # Professional services
    "law", "legal", "attorney", "lawyers", "lawyer",
    "accounting", "accountant", "bookkeeping",
    "consulting", "consultants", "advisor", "advisors",
    "staffing", "recruiting", "recruitment",
    "medical", "health", "healthcare", "dental", "dentist",
    "pharmacy", "pharma", "clinic", "care",
    # Retail / food
    "pawn", "jewelry", "jewelers",
    "fitness", "gym", "yoga", "wellness",
    "coffee", "cafe", "restaurant", "pizza", "sushi", "bakery", "food",
    "salon", "beauty", "spa", "massage",
    "print", "printing", "signs", "sign",
    "cleaning", "cleaner", "washers", "pressure", "washing",
    # Security / pest
    "security", "alarm", "pest", "control",
    # General
    "group", "services", "service", "solutions", "systems", "enterprises",
    "professionals", "associates", "partners", "international",
    "national", "global", "american", "usa",
}

# Legal suffixes / noise stripped before extracting brand words
NOISE_WORDS = {
    "llc", "inc", "corp", "ltd", "co", "the", "of", "and", "a", "an",
    "for", "in", "at", "by", "on", "to", "or", "pllc", "dba",
} | GENERIC_INDUSTRY_WORDS


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


def get_sheet_id(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def all_words(name):
    return [w for w in re.split(r"[\s,.\-&/()+]+", name.lower()) if len(w) >= 3]


def brand_words(name):
    """Words unique to this company — not noise/legal suffixes and not generic industry words."""
    return [w for w in all_words(name) if w not in NOISE_WORDS]


def is_skip_domain(url):
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    return any(s in domain for s in SKIP_DOMAINS)


def domain_matches_company(domain, company_name):
    """Domain must contain at least one brand word from the company name."""
    bwords = brand_words(company_name)
    if not bwords:
        return False  # all words are generic — can't safely match any domain
    domain_clean = domain.lower().replace("-", "").replace(".", "")
    return any(w in domain_clean for w in bwords)


def flush_updates(service, updates, sheet_id, tab, col_idx):
    if not updates:
        return
    data = [
        {"range": f"'{tab}'!{col_letter(col_idx)}{u['row']}", "values": [[u["url"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} websites", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Find verified company websites")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_name", type=int, default=0, help="Company name column (0-indexed)")
    parser.add_argument("--col_city", type=int, default=1)
    parser.add_argument("--col_state", type=int, default=2)
    parser.add_argument("--col_website", type=int, required=True, help="Column to write website")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    sheet_id = get_sheet_id(args.sheet_url)
    service = get_service()

    print("=== Find Verified Websites (brand-word strict) ===\n", flush=True)
    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{args.tab}'!A:AZ"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[args.col_name] if len(row) > args.col_name else ""
        city = row[args.col_city] if len(row) > args.col_city else ""
        state = row[args.col_state] if len(row) > args.col_state else ""
        website = row[args.col_website] if len(row) > args.col_website else ""
        if name.strip() and not website.strip():
            targets.append({"row": i+2, "name": name.strip(), "city": city.strip(), "state": state.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need website\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Searching Google ({len(targets)} queries)...", flush=True)
    queries, qmap = [], {}
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
            urls = [r.get("url", "") for r in item.get("organicResults", []) if r.get("url")]
            if q and urls:
                all_results[q] = urls
        print(f"  Batch {bn} done — {len(all_results)} results", flush=True)

    print(f"\n[3/3] Validating and writing...", flush=True)
    updates = []
    found = rejected = noresult = 0

    for q, t in qmap.items():
        urls = all_results.get(q, [])
        bw = brand_words(t["name"])
        chosen = None

        if not bw:
            noresult += 1
            print(f"  -  {t['name'][:45]:45s} -> (no brand words — skipped)", flush=True)
            continue

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
            print(f"  +  {t['name'][:45]:45s} -> {chosen[:55]}", flush=True)
        elif not urls:
            noresult += 1
        else:
            rejected += 1
            print(f"  x  {t['name'][:45]:45s} -> (no brand domain match — skipped)", flush=True)

        if len(updates) >= BATCH:
            flush_updates(service, updates, sheet_id, args.tab, args.col_website)
            updates = []

    if updates:
        flush_updates(service, updates, sheet_id, args.tab, args.col_website)

    print(f"\nSummary: Found {found}, Rejected {rejected}, No results/brand {noresult} / {len(targets)}", flush=True)


if __name__ == "__main__":
    main()
