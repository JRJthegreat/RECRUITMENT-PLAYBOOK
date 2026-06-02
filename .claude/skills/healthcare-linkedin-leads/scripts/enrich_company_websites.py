"""
Enrich the external LinkedIn-jobs sheet with company website / size / description.

Source sheet schema (20 cols, A-T) from the LinkedIn job scraper export:
  A Title  C Primary Description (company)  M Company Name  T aboutLink (LinkedIn company URL)

The aboutLink looks like  https://www.linkedin.com/company/lyra-health/about
The trailing '/about' is stripped before scraping (the profile actor needs the
bare company URL).

Calls pratikdani~linkedin-company-profile-scraper once per UNIQUE company URL
(dedup saves Apify credits) and writes back to NEW columns:
  U Company Website   V Company Size   W Company Description

Resume-safe: skips rows whose col U already has a non-LinkedIn value.

Usage:
  python3 -W ignore enrich_company_websites.py --sheet_url "URL" [--apply] [--workers 5] [--limit N]
    --limit N   cap number of UNIQUE companies scraped (for a small test run)
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

# Source columns (0-based)
COL_COMPANY_NAME = 12   # M
COL_ABOUT_LINK = 19     # T — LinkedIn company URL ending in /about
# Destination columns (0-based) — new
COL_WEBSITE = 20        # U
COL_SIZE = 21           # V
COL_DESC = 22           # W

NEW_HEADERS = {COL_WEBSITE: "Company Website", COL_SIZE: "Company Size", COL_DESC: "Company Description"}
BATCH_SIZE = 10


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def get_gid_from_url(url):
    import re
    m = re.search(r"[#&?]gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"]
    return meta["sheets"][0]["properties"]["title"]


def normalize_company_url(u):
    """Strip trailing /about (and trailing slash) so the actor gets the bare company URL."""
    u = (u or "").strip()
    if not u:
        return ""
    u = u.rstrip("/")
    if u.lower().endswith("/about"):
        u = u[: -len("/about")]
    return u


def is_linkedin_url(url):
    return "linkedin.com" in (url or "").lower()


def scrape_company(linkedin_url, timeout=90):
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
    website = (item.get("website") or "").strip()
    size = (item.get("company_size") or "").strip()
    description = (item.get("description") or "").strip()
    return website, size, description


def write_batch(service, sheet_id, updates):
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
    parser = argparse.ArgumentParser(description="Enrich LinkedIn jobs sheet with company website")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Write to sheet. Default: dry run.")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="Cap unique companies scraped (test runs)")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        sys.exit(1)

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab = resolve_tab(service, sheet_id, args.sheet_url)
    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Enrich Company Websites ({mode}) ===")
    print(f"Tab: {tab!r}\n")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A2:W10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Data rows: {len(rows)}")

    # Build map: normalized company url → list of (sheet_row)
    url_to_rows = {}
    skipped_already = 0
    skipped_no_url = 0
    for i, row in enumerate(rows):
        sheet_row = i + 2
        existing = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        if existing and not is_linkedin_url(existing):
            skipped_already += 1
            continue
        raw = row[COL_ABOUT_LINK] if len(row) > COL_ABOUT_LINK else ""
        url = normalize_company_url(raw)
        if not url or not is_linkedin_url(url):
            skipped_no_url += 1
            continue
        url_to_rows.setdefault(url, []).append((i, sheet_row))

    print(f"Already enriched (col U set): {skipped_already}")
    print(f"No usable LinkedIn URL:       {skipped_no_url}")
    print(f"Unique companies to scrape:   {len(url_to_rows)}")

    if args.limit and len(url_to_rows) > args.limit:
        kept = dict(list(url_to_rows.items())[: args.limit])
        print(f"  (--limit {args.limit} → scraping first {len(kept)} only)")
        url_to_rows = kept

    if not url_to_rows:
        print("\nNothing to do.")
        return

    if not args.apply:
        print("\nSample (stripped → will scrape):")
        for url, rl in list(url_to_rows.items())[:8]:
            company = rows[rl[0][0]][COL_COMPANY_NAME] if len(rows[rl[0][0]]) > COL_COMPANY_NAME else "?"
            print(f"  {company!r:32s}  {url}")
        print(f"\n[DRY RUN] Re-run with --apply to scrape {len(url_to_rows)} companies.")
        return

    # Ensure new headers exist
    write_batch(service, sheet_id, [
        {"range": f"'{tab}'!{col_letter(c)}1", "values": [[h]]}
        for c, h in NEW_HEADERS.items()
    ])

    results = {}
    found = not_found = 0
    urls = list(url_to_rows.keys())
    done = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut_to_url = {pool.submit(scrape_company, u): u for u in urls}
        for fut in as_completed(fut_to_url):
            url = fut_to_url[fut]
            done += 1
            try:
                item = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(urls)}] ERROR: {e}")
                not_found += 1
                continue
            company = rows[url_to_rows[url][0][0]][COL_COMPANY_NAME] if len(rows[url_to_rows[url][0][0]]) > COL_COMPANY_NAME else "?"
            if item:
                website, size, description = extract_fields(item)
                results[url] = (website, size, description)
                print(f"  [{done}/{len(urls)}] {company!r:30s}  site={website[:45]}")
                found += 1
            else:
                print(f"  [{done}/{len(urls)}] {company!r:30s}  NOT FOUND")
                not_found += 1

    print("\nWriting results...")
    pending = []
    written_rows = 0
    for url, rl in url_to_rows.items():
        if url not in results:
            continue
        website, size, description = results[url]
        for i, sheet_row in rl:
            if website:
                pending.append({"range": f"'{tab}'!{col_letter(COL_WEBSITE)}{sheet_row}", "values": [[website]]})
            if size:
                pending.append({"range": f"'{tab}'!{col_letter(COL_SIZE)}{sheet_row}", "values": [[size]]})
            if description:
                pending.append({"range": f"'{tab}'!{col_letter(COL_DESC)}{sheet_row}", "values": [[description]]})
            written_rows += 1
        if len(pending) >= BATCH_SIZE * 3:
            write_batch(service, sheet_id, pending)
            pending = []
            time.sleep(1.0)
    if pending:
        write_batch(service, sheet_id, pending)

    elapsed = int(time.time() - t0)
    print(f"\n=== Summary ===")
    print(f"Companies found:     {found}")
    print(f"Companies not found: {not_found}")
    print(f"Rows updated:        {written_rows}")
    print(f"Elapsed:             {elapsed}s")


if __name__ == "__main__":
    main()
