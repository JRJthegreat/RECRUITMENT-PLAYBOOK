"""
Enrich company profiles for all companies that have a website.

Step A — Find LinkedIn URL (for companies missing it):
  1. Scrape company website footer/header for linkedin.com/company/ links (free)
  2. Fallback: Google "{name}" site:linkedin.com/company/ → validate slug

Step B — Scrape harvestapi/linkedin-company (for companies with LinkedIn URL):
  Extract: employeeCount, employeeCountRange, description/about
  Write: employee_count → col P (15), company_about → col Q (16)

Skips companies already having both employee_count and company_about.

Run:
  python3 -W ignore enrich_company_profiles.py [--limit N] [--step {a,b,both}]
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse, urljoin
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

APIFY_TOKEN    = os.getenv("APIFY_API_TOKEN")
APIFY_BASE     = "https://api.apify.com/v2"
APIFY_GOOGLE   = "apify~google-search-scraper"
APIFY_LINKEDIN = "harvestapi~linkedin-company"

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"

COL_NAME     = 2   # C
COL_LINKEDIN = 5   # F
COL_WEBSITE  = 11  # L
COL_EMPLOYEE = 15  # P
COL_ABOUT    = 16  # Q

APIFY_BATCH  = 50
WRITE_BATCH  = 10
WEB_WORKERS  = 10
FETCH_TIMEOUT = 10

STOP_TOKENS = {
    "llc", "inc", "corp", "ltd", "the", "and", "company", "group",
    "healthcare", "health", "medical", "staffing", "solutions", "services",
    "international", "global", "national", "partners",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def company_tokens(name):
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in STOP_TOKENS and len(t) >= 3]


def clean_name(name):
    return re.sub(r"\s+(llc|inc|corp|ltd|lp|llp)\.?$", "", name.strip(), flags=re.IGNORECASE).strip()


def linkedin_slug(url):
    try:
        m = re.search(r"/company/([^/?#]+)", urlparse(url).path, re.IGNORECASE)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def slug_matches(slug, tokens):
    if not slug or not tokens:
        return False
    slug_chars = re.sub(r"[^a-z0-9]", "", slug)
    return any(t in slug_chars for t in tokens)


def normalize_linkedin(url):
    m = re.search(r"(linkedin\.com/company/[^/?#\s]+)", url, re.IGNORECASE)
    return "https://www." + m.group(1) if m else ""


# ── Google Sheets ─────────────────────────────────────────────────────────────

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
        if u.get("company_about"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_ABOUT)}{u['row']}",
                "values": [[u["company_about"]]],
            })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    time.sleep(1)


# ── Step A: Find LinkedIn URL ─────────────────────────────────────────────────

def scrape_website_for_linkedin(website_url, company_name):
    """Check the company's own website for a LinkedIn company page link."""
    tokens = company_tokens(company_name)
    try:
        resp = requests.get(website_url, headers=HEADERS, timeout=FETCH_TIMEOUT,
                            allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "linkedin.com/company/" not in href.lower():
                continue
            # Make absolute if relative
            if href.startswith("/"):
                href = urljoin(website_url, href)
            slug = linkedin_slug(href)
            if slug:
                # Accept if slug matches OR if it's clearly a company page
                if slug_matches(slug, tokens) or slug:
                    return normalize_linkedin(href)
    except Exception:
        pass
    return ""


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
    except requests.exceptions.Timeout:
        print("  [!] Google timeout", flush=True)
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] Google HTTP {resp.status_code}", flush=True)
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def find_linkedin_from_google(organic, company_name):
    tokens = company_tokens(company_name)
    for r in organic:
        url = r.get("url", "") or ""
        if "linkedin.com/company/" not in url.lower():
            continue
        slug = linkedin_slug(url)
        if slug_matches(slug, tokens):
            return normalize_linkedin(url)
    return ""


# ── Step B: harvestapi scrape ─────────────────────────────────────────────────

def scrape_harvestapi(linkedin_url):
    """Returns dict with employee_count and company_about, or None on error."""
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_LINKEDIN}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"companies": [linkedin_url]},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"    [!] harvestapi error: {e}", flush=True)
        return None
    if resp.status_code not in (200, 201):
        print(f"    [!] harvestapi HTTP {resp.status_code}", flush=True)
        return None
    items = resp.json()
    if not items:
        return None
    item = items[0]
    if item.get("error"):
        return None

    # Employee count
    emp_count = item.get("employeeCount")
    emp_range = item.get("employeeCountRange", "")
    if emp_count is not None:
        employee_str = str(emp_count)
    elif emp_range:
        employee_str = str(emp_range)
    else:
        employee_str = ""

    # About/description — try all known field names
    about = ""
    for field in ("description", "about", "companyDescription", "overview", "tagline", "summary"):
        val = item.get(field, "")
        if val and isinstance(val, str) and len(val.strip()) > 20:
            about = val.strip()[:1500]
            break

    return {"employee_count": employee_str, "company_about": about, "_raw_keys": list(item.keys())}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--step", choices=["a", "b", "both"], default="both")
    parser.add_argument("--debug-fields", action="store_true",
                        help="Print raw harvestapi field names for first company")
    args = parser.parse_args()

    service = get_service()

    # Ensure headers exist
    for col_idx, header in [(COL_EMPLOYEE, "employee_count"), (COL_ABOUT, "company_about")]:
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{TAB_NAME}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:R"
    ).execute().get("values", [])[1:]

    # Build target lists
    need_linkedin = []   # has website, no linkedin
    need_enrichment = [] # has linkedin, missing employee or about

    for i, row in enumerate(rows):
        name     = row[COL_NAME]     if len(row) > COL_NAME     else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        website  = row[COL_WEBSITE]  if len(row) > COL_WEBSITE  else ""
        employee = row[COL_EMPLOYEE] if len(row) > COL_EMPLOYEE else ""
        about    = row[COL_ABOUT]    if len(row) > COL_ABOUT    else ""

        if not name.strip() or not website.strip():
            continue

        t = {"row": i + 2, "name": name.strip(), "website": website.strip(),
             "linkedin_url": linkedin.strip()}

        if not linkedin.strip():
            need_linkedin.append(t)
        elif not employee.strip() or not about.strip():
            need_enrichment.append(t)

    if args.limit:
        need_linkedin   = need_linkedin[:args.limit]
        need_enrichment = need_enrichment[:args.limit]

    print(f"=== Enrich Company Profiles ===", flush=True)
    print(f"  Need LinkedIn URL : {len(need_linkedin)}", flush=True)
    print(f"  Need enrichment   : {len(need_enrichment)}", flush=True)
    print(flush=True)

    updates  = []
    li_found = li_miss = 0

    # ── Step A: Find LinkedIn ─────────────────────────────────────────────────
    if args.step in ("a", "both") and need_linkedin:
        print(f"[Step A1] Website scrape for LinkedIn links — {len(need_linkedin)} companies...\n", flush=True)

        def scrape_job(t):
            return t, scrape_website_for_linkedin(t["website"], t["name"])

        need_google = []
        with ThreadPoolExecutor(max_workers=WEB_WORKERS) as ex:
            futures = {ex.submit(scrape_job, t): t for t in need_linkedin}
            for fut in as_completed(futures):
                t, li_url = fut.result()
                if li_url:
                    li_found += 1
                    print(f"  +WEB {t['name'][:50]:50s} → {li_url[:60]}", flush=True)
                    t["linkedin_url"] = li_url
                    updates.append({"row": t["row"], "linkedin_url": li_url})
                    need_enrichment.append(t)
                else:
                    need_google.append(t)

                if len(updates) >= WRITE_BATCH:
                    flush(service, updates)
                    updates = []

        if updates:
            flush(service, updates)
            updates = []

        print(f"\n  Website scrape found: {li_found}/{len(need_linkedin)}", flush=True)
        print(f"  Falling back to Google for: {len(need_google)}\n", flush=True)

        # Google fallback
        if need_google:
            print(f"[Step A2] Google fallback — {len(need_google)} companies...\n", flush=True)
            queries = [f'{clean_name(t["name"])} site:linkedin.com/company/' for t in need_google]
            qmap = dict(zip(queries, need_google))
            total_batches = (len(queries) + APIFY_BATCH - 1) // APIFY_BATCH

            for b in range(0, len(queries), APIFY_BATCH):
                batch = queries[b:b + APIFY_BATCH]
                bn = b // APIFY_BATCH + 1
                print(f"  Batch {bn}/{total_batches}...", flush=True)
                results = apify_google_search(batch)

                for q in batch:
                    t = qmap.get(q)
                    if not t:
                        continue
                    li_url = find_linkedin_from_google(results.get(q, []), t["name"])
                    if li_url:
                        li_found += 1
                        print(f"  +GGL {t['name'][:50]:50s} → {li_url[:60]}", flush=True)
                        t["linkedin_url"] = li_url
                        updates.append({"row": t["row"], "linkedin_url": li_url})
                        need_enrichment.append(t)
                    else:
                        li_miss += 1

                    if len(updates) >= WRITE_BATCH:
                        flush(service, updates)
                        updates = []

                print(f"  Batch {bn} done — {li_found} LinkedIn URLs found so far", flush=True)

            if updates:
                flush(service, updates)
                updates = []

        print(f"\n  Step A total — found: {li_found}, not found: {li_miss}\n", flush=True)

    # ── Step B: harvestapi enrichment ─────────────────────────────────────────
    if args.step in ("b", "both") and need_enrichment:
        print(f"[Step B] harvestapi scrape — {len(need_enrichment)} companies...\n", flush=True)
        enriched = failed = 0
        debug_done = False

        for t in need_enrichment:
            data = scrape_harvestapi(t["linkedin_url"])

            if data:
                if args.debug_fields and not debug_done:
                    print(f"  [DEBUG] harvestapi fields: {data['_raw_keys']}", flush=True)
                    debug_done = True

                enriched += 1
                emp = data.get("employee_count", "")
                about = data.get("company_about", "")
                print(
                    f"  + {t['name'][:48]:48s} | {emp or '?':>10} | {(about[:40] + '…') if about else '(no about)'}",
                    flush=True,
                )
                updates.append({
                    "row": t["row"],
                    "employee_count": emp if emp else None,
                    "company_about": about if about else None,
                })
            else:
                failed += 1
                print(f"  x {t['name'][:50]:50s} → (scrape failed)", flush=True)

            if len(updates) >= WRITE_BATCH:
                flush(service, updates)
                updates = []

            time.sleep(0.3)

        if updates:
            flush(service, updates)

        print(f"\n  Step B — enriched: {enriched}, failed: {failed} / {len(need_enrichment)}", flush=True)

    print(f"\n=== Done ===", flush=True)


if __name__ == "__main__":
    main()
