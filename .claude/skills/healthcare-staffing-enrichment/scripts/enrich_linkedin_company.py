"""
Phase 2: Enrich LinkedIn company URL and employee count.

Step A: Google Search → find LinkedIn company page URL per company.
Step B: harvestapi/linkedin-company → confirm URL + get employeeCount/employeeCountRange.

Writes:
  - linkedin_url → col G (index 6)
  - employee_count → col O (index 14, new)

Run:
  python3 -W ignore enrich_linkedin_company.py --sheet_url "URL" [--limit N]
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse, unquote
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
APIFY_GOOGLE = "apify~google-search-scraper"
APIFY_LINKEDIN = "harvestapi~linkedin-company"

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_NAME = 3         # D: company_name
COL_LINKEDIN = 6     # G: linkedin_url
COL_EMPLOYEE = 16    # Q: employee_count (new; O+P taken by classification)

WRITE_BATCH = 10
GOOGLE_BATCH = 50

STOP_TOKENS = {
    "llc", "inc", "corp", "ltd", "the", "and", "company", "group",
    "healthcare", "health", "medical", "staffing", "solutions", "services",
    "international", "global", "national", "partners",
}


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


def ensure_header(service):
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB_NAME}'!{col_letter(COL_EMPLOYEE)}1",
        valueInputOption="RAW",
        body={"values": [["employee_count"]]},
    ).execute()
    print("  Set header: employee_count")


def company_tokens(name):
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in STOP_TOKENS and len(t) >= 3]


def linkedin_slug(url):
    try:
        path = urlparse(url).path
        m = re.search(r"/company/([^/?#]+)", path, re.IGNORECASE)
        if not m:
            return ""
        return unquote(m.group(1)).lower()
    except Exception:
        return ""


def slug_matches(slug, tokens):
    if not slug or not tokens:
        return False
    slug_chars = re.sub(r"[^a-z0-9]", "", slug)
    return any(t in slug_chars for t in tokens)


def clean_company_name(name):
    return re.sub(r"\s+(llc|inc|corp|ltd|lp|llp)\.?$", "", name.strip(), flags=re.IGNORECASE).strip()


def apify_google_search(queries):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_GOOGLE}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"queries": "\n".join(queries), "resultsPerPage": 5,
                  "maxPagesPerQuery": 1, "languageCode": "en",
                  "countryCode": "us", "includeUnfilteredResults": False},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"  [!] Google search error: {e}")
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] HTTP {resp.status_code}: {resp.text[:200]}")
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def find_linkedin_url(organic, company_name):
    """Extract a validated LinkedIn company URL from Google search results."""
    tokens = company_tokens(company_name)
    for r in organic:
        url = r.get("url", "") or ""
        if "linkedin.com/company/" not in url.lower():
            continue
        slug = linkedin_slug(url)
        if slug_matches(slug, tokens):
            # Normalize to canonical form
            path_match = re.search(r"(linkedin\.com/company/[^/?#]+)", url, re.IGNORECASE)
            if path_match:
                return "https://www." + path_match.group(1)
    return None


def scrape_linkedin_company(linkedin_url):
    """Call harvestapi/linkedin-company to get employee count + confirm URL.
    Returns dict with employeeCount, employeeCountRange, linkedinUrl or None on error.
    """
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_LINKEDIN}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"companies": [linkedin_url]},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"    [!] Apify error: {e}")
        return None
    if resp.status_code not in (200, 201):
        print(f"    [!] Apify HTTP {resp.status_code}: {resp.text[:150]}")
        return None
    items = resp.json()
    if not items:
        return None
    item = items[0]
    if item.get("error"):
        return None
    return item


def flush_updates(service, updates):
    if not updates:
        return
    data = []
    for u in updates:
        if u.get("linkedin_url"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN)}{u['row']}",
                "values": [[u["linkedin_url"]]],
            })
        if u.get("employee_count") is not None:
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_EMPLOYEE)}{u['row']}",
                "values": [[u["employee_count"]]],
            })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    print(f"  -> Wrote {len(updates)} companies", flush=True)
    time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", default=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); return

    service = get_service()
    ensure_header(service)

    print("=== Phase 2: LinkedIn Company Enrichment ===\n", flush=True)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        if name.strip() and not linkedin.strip():
            targets.append({"row": i + 2, "name": name.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need LinkedIn enrichment\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    # Step A: Google Search for LinkedIn URLs — write each batch immediately
    if targets:
        print(f"[Step A] Google Search — {len(targets)} companies need LinkedIn URL...", flush=True)
        queries = [f'{clean_company_name(t["name"])} site:linkedin.com/company/' for t in targets]
        qmap = dict(zip(queries, targets))
        total_batches = (len(queries) + GOOGLE_BATCH - 1) // GOOGLE_BATCH
        found_url = 0
        url_updates = []

        for b in range(0, len(queries), GOOGLE_BATCH):
            batch = queries[b:b + GOOGLE_BATCH]
            bn = b // GOOGLE_BATCH + 1
            print(f"  Batch {bn}/{total_batches}...", flush=True)
            batch_results = apify_google_search(batch)

            for q in batch:
                t = qmap.get(q)
                if not t:
                    continue
                url = find_linkedin_url(batch_results.get(q, []), t["name"])
                if url:
                    found_url += 1
                    url_updates.append({"row": t["row"], "linkedin_url": url, "employee_count": None})
                    print(f"  +  {t['name'][:50]:50s} → {url[:60]}", flush=True)
                else:
                    print(f"  x  {t['name'][:50]:50s} → (not found)", flush=True)

                if len(url_updates) >= WRITE_BATCH:
                    flush_updates(service, url_updates)
                    url_updates = []

            print(f"  Batch {bn} done — {found_url} URLs found so far", flush=True)

        if url_updates:
            flush_updates(service, url_updates)
        print(f"\n  LinkedIn URLs found: {found_url}/{len(targets)}\n", flush=True)
    else:
        print("[Step A] All companies already have LinkedIn URL — skipping.\n", flush=True)

    # Re-read: Step B targets = linkedin_url set but employee_count empty
    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    step_b = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        employee = row[COL_EMPLOYEE] if len(row) > COL_EMPLOYEE else ""
        if name.strip() and linkedin.strip() and not employee.strip():
            step_b.append({"row": i + 2, "name": name.strip(), "linkedin_url": linkedin.strip()})

    if args.limit:
        step_b = step_b[:args.limit]

    if not step_b:
        print("[Step B] All companies already have employee count — nothing to do."); return

    print(f"[Step B] Scraping LinkedIn company details — {len(step_b)} companies...", flush=True)
    updates = []
    enriched = failed = 0

    for t in step_b:
        data = scrape_linkedin_company(t["linkedin_url"])
        employee_val = ""
        confirmed_url = t["linkedin_url"]

        if data:
            enriched += 1
            emp_count = data.get("employeeCount")
            emp_range = data.get("employeeCountRange", "")
            confirmed_url = data.get("linkedinUrl") or t["linkedin_url"]
            if emp_count is not None:
                employee_val = str(emp_count)
            elif emp_range:
                employee_val = emp_range
            print(f"  +  {t['name'][:50]:50s} → {employee_val or 'no count'}", flush=True)
        else:
            failed += 1
            print(f"  x  {t['name'][:50]:50s} → (scrape failed)", flush=True)

        updates.append({
            "row": t["row"],
            "linkedin_url": confirmed_url,
            "employee_count": employee_val if employee_val else None,
        })

        if len(updates) >= WRITE_BATCH:
            flush_updates(service, updates)
            updates = []

        time.sleep(0.5)

    if updates:
        flush_updates(service, updates)

    print(f"\nSummary: enriched {enriched}, failed {failed} / {len(step_b)}", flush=True)


if __name__ == "__main__":
    main()
