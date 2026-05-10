"""
Phase 2: Find decision makers for SBA rural borrowers via Google + LinkedIn.

For each borrower with a website (col M), searches Google for:
  "{company_name}" ("CEO" OR "Owner" OR "President" OR "Founder") site:linkedin.com/in/

Parses name + title from Google snippet. All borrowers target CEO/Owner level
regardless of size since these are small rural businesses.

Reads:  col A=borrower_name, col M=website (to confirm we have a domain)
Writes: col N=dm_name, col O=dm_title, col P=dm_linkedin

Skips rows that already have dm_name. Resumable.

Run:
  python3 -W ignore find_borrower_dms.py [--limit N] [--retry]
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
COL_NAME = 0; COL_WEBSITE = 12; COL_DM_NAME = 13; COL_DM_TITLE = 14; COL_DM_LINKEDIN = 15

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 1

CEO_KEYWORDS = [
    "ceo", "president", "owner", "founder", "managing director",
    "co-founder", "cofounder", "chief executive", "principal",
    "proprietor", "managing member", "general manager", "director",
    "managing partner", "partner",
]


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


def validate_ceo(name, title):
    if not name or not title:
        return False
    return any(kw in title.lower() for kw in CEO_KEYWORDS)


def flush_updates(service, updates):
    if not updates:
        return
    data = []
    for u in updates:
        for col_idx, val in u["cells"].items():
            data.append({"range": f"'{TAB}'!{col_letter(col_idx)}{u['row']}", "values": [[val]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} DMs", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    service = get_service()
    print("=== Find Borrower Decision Makers (Google + LinkedIn) ===\n", flush=True)

    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:P"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        dm = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""
        if name.strip() and website.strip() and not dm.strip():
            targets.append({"row": i+2, "name": name.strip(), "website": website.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} borrowers need DM lookup (have website, no DM yet)\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Searching Google ({len(targets)} queries)...", flush=True)
    queries = []
    qmap = {}
    for t in targets:
        q = f'"{t["name"]}" ("CEO" OR "Owner" OR "President" OR "Founder" OR "Managing Director") site:linkedin.com/in/'
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
            if validate_ceo(pname, ptitle):
                best_name, best_title, best_url = pname, ptitle, purl
                break

        if best_name:
            found += 1
            updates.append({
                "row": t["row"],
                "cells": {COL_DM_NAME: best_name, COL_DM_TITLE: best_title or "", COL_DM_LINKEDIN: best_url or ""}
            })
            print(f"  +  {t['name'][:40]:40s} -> {best_name} ({best_title})", flush=True)
        else:
            nf += 1
            print(f"  x  {t['name'][:40]:40s} -> (not found)", flush=True)

        if len(updates) >= BATCH:
            flush_updates(service, updates)
            updates = []

    if updates:
        flush_updates(service, updates)

    print(f"\nSummary: Found {found}/{len(targets)}, Not found {nf}", flush=True)


if __name__ == "__main__":
    main()
