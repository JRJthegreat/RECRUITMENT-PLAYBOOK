"""
Verify websites already in col M — label each "correct" or "not_correct" in col V.

Logic (strict):
  1. Domain contains a brand word from company name → correct
  2. Fetch page; brand word in <title> tag → correct
  3. Anything else → not_correct

Run:
  python3 -W ignore verify_websites.py [--limit N]
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from bs4 import BeautifulSoup
except ImportError:
    import sys; print("pip3 install beautifulsoup4 lxml"); sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_NAME    = 3   # D
COL_WEBSITE = 12  # M
COL_RESULT  = 21  # V: website_verified

WRITE_BATCH = 10
WORKERS     = 10
TIMEOUT     = 10

GENERIC_WORDS = {
    "healthcare", "health", "medical", "staffing", "staff", "nursing",
    "nurse", "nurses", "clinical", "care", "therapy", "therapist",
    "allied", "locum", "travel", "per", "diem", "agency", "agencies",
    "solutions", "services", "service", "group", "partners", "professionals",
    "associates", "international", "national", "global", "american", "usa",
    "workforce", "network", "resources", "management", "consulting",
    "the", "of", "and", "a", "an", "for", "in", "at", "by", "on",
    "llc", "inc", "corp", "ltd", "dba", "aka", "company", "co",
    "plus", "pro", "premier", "elite", "first", "best", "top",
    "home", "quality", "professional", "certified", "licensed",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def brand_words(name):
    words = re.split(r"[\s,.\-&/()+\'\"]+", name.lower())
    return [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]


def base_domain(url):
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\d*\.", "", host)
    except Exception:
        return ""


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


def flush(service, updates):
    if not updates:
        return
    data = [
        {"range": f"'{TAB_NAME}'!{col_letter(COL_RESULT)}{u['row']}",
         "values": [[u["label"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    time.sleep(0.5)


def check(row_num, name, url):
    bwords = brand_words(name)
    if not bwords:
        return row_num, "not_correct"

    domain = base_domain(url)
    domain_clean = domain.replace("-", "").replace(".", "")
    if any(w in domain_clean for w in bwords):
        return row_num, "correct"

    # Domain doesn't match — fetch and check title only
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return row_num, "not_correct"
        soup = BeautifulSoup(resp.text, "lxml")
        title = (soup.title.string or "").lower() if soup.title else ""
        if any(w in title for w in bwords):
            return row_num, "correct"
    except Exception:
        pass

    return row_num, "not_correct"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    service = get_service()

    # Write header
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB_NAME}'!{col_letter(COL_RESULT)}1",
        valueInputOption="RAW",
        body={"values": [["website_verified"]]},
    ).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name    = row[COL_NAME]    if len(row) > COL_NAME    else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        if name.strip() and website.strip():
            targets.append((i + 2, name.strip(), website.strip()))

    if args.limit:
        targets = targets[:args.limit]

    print(f"=== Verifying {len(targets)} websites ===\n", flush=True)
    correct = not_correct = 0
    updates = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check, r, n, u): (r, n, u) for r, n, u in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            row_num, label = fut.result()
            _, name, url = futures[fut]
            if label == "correct":
                correct += 1
            else:
                not_correct += 1
                print(f"  NOT_CORRECT  {name[:55]:55s} → {url[:50]}", flush=True)
            updates.append({"row": row_num, "label": label})
            if len(updates) >= WRITE_BATCH:
                flush(service, updates)
                updates = []
            if i % 100 == 0:
                print(f"  [{i}/{len(targets)}] correct={correct} not_correct={not_correct}", flush=True)

    if updates:
        flush(service, updates)

    print(f"\n=== Done: {correct} correct, {not_correct} not_correct / {len(targets)} ===", flush=True)


if __name__ == "__main__":
    main()
