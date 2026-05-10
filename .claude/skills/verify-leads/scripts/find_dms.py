"""
Find decision makers via Google + LinkedIn for companies with a known website.

Searches Google for:
  "{company}" ("CEO" OR "Owner" OR "President" OR "Founder") site:linkedin.com/in/

Parses name + title from the Google snippet. Target role is configurable
via --target but defaults to CEO/Owner (right for most small businesses).

Run:
  python3 -W ignore find_dms.py \
    --sheet_url "URL" --tab "TAB" \
    --col_name 0 --col_website 12 \
    --col_dm_name 13 --col_dm_title 14 --col_dm_linkedin 15
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

TARGET_QUERIES = {
    "ceo": '"CEO" OR "Owner" OR "President" OR "Founder" OR "Managing Director" OR "Principal"',
    "vp":  '"VP" OR "Vice President" OR "SVP" OR "Head of" OR "Director"',
    "hr":  '"HR Director" OR "Head of People" OR "VP of HR" OR "CHRO" OR "People Operations"',
}

TARGET_KEYWORDS = {
    "ceo": [
        "ceo", "president", "owner", "founder", "managing director",
        "co-founder", "cofounder", "chief executive", "principal",
        "proprietor", "managing member", "general manager", "managing partner", "partner",
    ],
    "vp": [
        "vp", "vice president", "svp", "head of", "director",
        "evp", "executive vice president",
    ],
    "hr": [
        "hr director", "head of people", "vp of hr", "chro", "chief people",
        "people operations", "talent acquisition",
    ],
}


def col_letter(idx):
    if idx < 26: return chr(65 + idx)
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


def parse_linkedin_result(result):
    title = result.get("title", "")
    url = result.get("url", "")
    if "linkedin.com/in/" not in url:
        return None, None, None
    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    if len(parts) >= 2:
        name = parts[0].strip()
        role = re.sub(r"\s+at\s+.*$", "", parts[1].strip(), flags=re.IGNORECASE).strip()
        return name, role, url
    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url
    return None, None, url


def validate_title(title, target):
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in TARGET_KEYWORDS.get(target, TARGET_KEYWORDS["ceo"]))


def flush_updates(service, updates, sheet_id, tab, col_name, col_title, col_li):
    if not updates:
        return
    data = []
    for u in updates:
        data += [
            {"range": f"'{tab}'!{col_letter(col_name)}{u['row']}", "values": [[u["name"]]]},
            {"range": f"'{tab}'!{col_letter(col_title)}{u['row']}", "values": [[u["title"]]]},
            {"range": f"'{tab}'!{col_letter(col_li)}{u['row']}", "values": [[u["linkedin"]]]},
        ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} DMs", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Find DMs via Google + LinkedIn")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_name", type=int, default=0)
    parser.add_argument("--col_website", type=int, required=True)
    parser.add_argument("--col_dm_name", type=int, required=True)
    parser.add_argument("--col_dm_title", type=int, required=True)
    parser.add_argument("--col_dm_linkedin", type=int, required=True)
    parser.add_argument("--target", choices=["ceo", "vp", "hr"], default="ceo")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    sheet_id = get_sheet_id(args.sheet_url)
    service = get_service()

    print(f"=== Find Decision Makers (Google + LinkedIn, target={args.target}) ===\n", flush=True)
    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{args.tab}'!A:AZ"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[args.col_name] if len(row) > args.col_name else ""
        website = row[args.col_website] if len(row) > args.col_website else ""
        dm = row[args.col_dm_name] if len(row) > args.col_dm_name else ""
        if name.strip() and website.strip() and not dm.strip():
            targets.append({"row": i+2, "name": name.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need DM (have website, no DM yet)\n", flush=True)
    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Searching Google ({len(targets)} queries)...", flush=True)
    queries, qmap = [], {}
    q_template = TARGET_QUERIES[args.target]
    for t in targets:
        q = f'"{t["name"]}" ({q_template}) site:linkedin.com/in/'
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
            if q and organic:
                all_results[q] = organic
        print(f"  Batch {bn} done — {len(all_results)} results", flush=True)

    print(f"\n[3/3] Matching DMs...", flush=True)
    updates = []
    found = nf = 0

    for q, t in qmap.items():
        organic = all_results.get(q, [])
        best_name = best_title = best_url = None
        for r in organic:
            pname, ptitle, purl = parse_linkedin_result(r)
            if pname and validate_title(ptitle, args.target):
                best_name, best_title, best_url = pname, ptitle, purl
                break

        if best_name:
            found += 1
            updates.append({"row": t["row"], "name": best_name,
                             "title": best_title or "", "linkedin": best_url or ""})
            print(f"  +  {t['name'][:40]:40s} -> {best_name} ({best_title})", flush=True)
        else:
            nf += 1

        if len(updates) >= BATCH:
            flush_updates(service, updates, sheet_id, args.tab,
                          args.col_dm_name, args.col_dm_title, args.col_dm_linkedin)
            updates = []

    if updates:
        flush_updates(service, updates, sheet_id, args.tab,
                      args.col_dm_name, args.col_dm_title, args.col_dm_linkedin)

    print(f"\nSummary: Found {found}/{len(targets)}, Not found {nf}", flush=True)


if __name__ == "__main__":
    main()
