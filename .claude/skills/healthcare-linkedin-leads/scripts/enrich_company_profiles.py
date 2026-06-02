"""
Phase 1.85: Enrich company profiles via pratikdani~linkedin-company-profile-scraper.

Reads LinkedIn company URL (col L), calls actor, writes back:
  L: real company website
  M: company size (e.g. "51-200 employees")
  P: company description

Resume safety: skips rows where col L is already a non-LinkedIn URL (already enriched).

Usage:
  python3 -W ignore enrich_company_profiles.py --sheet_url "URL" [--apply] [--workers 5]
"""

import os
import sys
import json
import time
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ACTOR = "pratikdani~linkedin-company-profile-scraper"
SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"

TAB_NAME = "Leads"
COL_COMPANY_NAME = 10   # K (0-based)
COL_WEBSITE = 11        # L
COL_SIZE = 12           # M
COL_DESCRIPTION = 15    # P

BATCH_SIZE = 10


def is_linkedin_url(url):
    return "linkedin.com" in (url or "").lower()


def get_google_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"], client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def scrape_company(linkedin_url, timeout=90):
    """Call actor for one LinkedIn company URL. Returns dict or None."""
    try:
        resp = requests.post(
            SYNC_URL,
            params={"token": APIFY_TOKEN, "limit": 1},
            json={"url": linkedin_url},
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"  [!] {linkedin_url}: {type(e).__name__}: {e}")
        return None

    if resp.status_code not in (200, 201):
        print(f"  [!] {linkedin_url}: HTTP {resp.status_code}")
        return None

    try:
        data = resp.json()
        if not data:
            return None
        item = data[0]
        if item.get("error"):
            return None
        return item
    except Exception:
        return None


def extract_fields(item):
    """Pull website, size, description from actor output."""
    website = (item.get("website") or "").strip()
    size = (item.get("company_size") or "").strip()
    description = (item.get("description") or "").strip()
    return website, size, description


def write_batch(service, sheet_id, updates):
    """Write a batch of cell updates."""
    if not updates:
        return
    for attempt in range(6):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            return
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 503) and attempt < 5:
                time.sleep(4 * (2 ** attempt))
            else:
                raise


def main():
    parser = argparse.ArgumentParser(description="Enrich company profiles from LinkedIn")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Write to sheet. Default: dry run.")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        sys.exit(1)

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Enrich Company Profiles ({mode}) ===")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A2:AC10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    # Build map: linkedin_url → list of (row_index, sheet_row)
    url_to_rows = {}
    skipped_already = 0
    for i, row in enumerate(rows):
        sheet_row = i + 2
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        if not is_linkedin_url(website):
            skipped_already += 1
            continue
        url_to_rows.setdefault(website, []).append((i, sheet_row))

    print(f"Already enriched (non-LinkedIn URL in col L): {skipped_already}")
    print(f"Unique LinkedIn URLs to enrich: {len(url_to_rows)}\n")

    if not url_to_rows:
        print("Nothing to do.")
        return

    if not args.apply:
        sample = list(url_to_rows.items())[:5]
        print("Sample companies to enrich:")
        for url, row_list in sample:
            company = rows[row_list[0][0]][COL_COMPANY_NAME] if len(rows[row_list[0][0]]) > COL_COMPANY_NAME else "?"
            print(f"  {company!r:35s}  {url}")
        print(f"\n[DRY RUN] Re-run with --apply to enrich {len(url_to_rows)} companies.")
        return

    # Enrich in parallel
    results = {}  # linkedin_url → (website, size, description)
    found = 0
    not_found = 0
    t0 = time.time()

    urls = list(url_to_rows.keys())
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_url = {pool.submit(scrape_company, url): url for url in urls}
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            done += 1
            try:
                item = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(urls)}] ERROR: {e}")
                not_found += 1
                continue

            if item:
                website, size, description = extract_fields(item)
                results[url] = (website, size, description)
                company = rows[url_to_rows[url][0][0]][COL_COMPANY_NAME] if len(rows[url_to_rows[url][0][0]]) > COL_COMPANY_NAME else "?"
                print(f"  [{done}/{len(urls)}] {company!r:35s}  size={size!r:20s}  site={website[:40]}")
                found += 1
            else:
                company = rows[url_to_rows[url][0][0]][COL_COMPANY_NAME] if len(rows[url_to_rows[url][0][0]]) > COL_COMPANY_NAME else "?"
                print(f"  [{done}/{len(urls)}] {company!r:35s}  NOT FOUND")
                not_found += 1

    # Write back in batches
    print(f"\nWriting results to sheet...")
    pending_updates = []
    written_rows = 0

    for linkedin_url, row_list in url_to_rows.items():
        if linkedin_url not in results:
            continue
        website, size, description = results[linkedin_url]
        for i, sheet_row in row_list:
            col_l_a1 = f"'{TAB_NAME}'!L{sheet_row}"
            col_m_a1 = f"'{TAB_NAME}'!M{sheet_row}"
            col_p_a1 = f"'{TAB_NAME}'!P{sheet_row}"
            if website:
                pending_updates.append({"range": col_l_a1, "values": [[website]]})
            if size:
                pending_updates.append({"range": col_m_a1, "values": [[size]]})
            if description:
                pending_updates.append({"range": col_p_a1, "values": [[description]]})
            written_rows += 1

        if len(pending_updates) >= BATCH_SIZE * 3:
            write_batch(service, sheet_id, pending_updates)
            pending_updates = []
            time.sleep(1.0)

    if pending_updates:
        write_batch(service, sheet_id, pending_updates)

    elapsed = int(time.time() - t0)
    print(f"\n=== Summary ===")
    print(f"Companies found:     {found}")
    print(f"Companies not found: {not_found}")
    print(f"Rows updated:        {written_rows}")
    print(f"Elapsed:             {elapsed}s")


if __name__ == "__main__":
    main()
