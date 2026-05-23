"""
Phase 1: Find official websites for healthcare staffing agencies.

Two-stage pipeline:
  Stage A (free): Mine cached Google results from classify_agencies.py →
                  HTML-verify each candidate URL.
  Stage B (Apify): Fresh unquoted search with state/city context →
                   HTML-verify candidates.

Validation: fetch page, check if any non-generic brand word from company
name appears in title or body text. No domain-matching required.

Run:
  python3 -W ignore find_websites.py --sheet_url "URL" [--limit N]
                                     [--stage {a,b,both}]
"""

import os
import re
import sys
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
    print("ERROR: pip3 install beautifulsoup4 lxml"); sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache",
    "classify_search_1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4.json")

load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_ADDRESS = 0   # A: address
COL_CITY = 1      # B: city
COL_NAME = 3      # D: company_name
COL_STATE = 10    # K: state
COL_WEBSITE = 12  # M: website

WRITE_BATCH = 10
APIFY_BATCH = 50
HTML_WORKERS = 10
FETCH_TIMEOUT = 10

SKIP_DOMAINS = {
    # Job boards
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
    "careerbuilder.com", "jobs.com", "recruitingsite.com",
    "vivian.com", "nursefly.com", "bluepipes.com", "travelnursingcentral.com",
    "healthecareers.com", "nursefinders.com", "nursesnextdoor.com",
    # Social / professional networks
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com", "x.com",
    "bebee.com", "theorg.com",
    # Directories / aggregators
    "yelp.com", "bloomberg.com", "crunchbase.com", "zoominfo.com",
    "dnb.com", "bbb.org", "rocketreach.co", "apollo.io",
    "manta.com", "yellowpages.com", "chamberofcommerce.com",
    "opencorporates.com", "buzzfile.com", "dandb.com",
    "highergov.com", "govwin.com", "usaspending.gov", "sam.gov",
    "icij.org", "offshoreleaks.icij.org",
    # Website builders / hosting
    "wixsite.com", "weebly.com", "squarespace.com", "godaddysites.com",
    # ATS / recruiting platforms
    "workable.com", "greenhouse.io", "lever.co", "applytojob.com",
    "staffmarkgroup.com", "hirequest.com", "staffinghub.com",
    "staffingagencies.com", "vaia.com",
    # Staffing aggregators / marketplaces
    "aya.com", "amn.com",
    # Search engines
    "google.com", "bing.com", "yahoo.com",
    # Publishing / media
    "myamericannurse.com",
}

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
    "Accept-Language": "en-US,en;q=0.9",
}


def brand_words(name):
    words = re.split(r"[\s,.\-&/()+\'\"]+", name.lower())
    return [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]


def root_url(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def base_domain(url):
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        return re.sub(r"^www\d*\.", "", host)
    except Exception:
        return ""


def is_skip(url):
    d = base_domain(url)
    if d.endswith(".gov") or d.endswith(".edu"):
        return True
    return any(s == d or d.endswith("." + s) for s in SKIP_DOMAINS)


def html_verify(url, company_name):
    """Fetch URL and confirm company name appears in page content.

    Acceptance requires:
    1. Final URL not in SKIP_DOMAINS
    2. Brand word in domain OR final path is root-level (prevents directory hits)
    3. Brand word found in page title or body text
    """
    bwords = brand_words(company_name)
    if not bwords:
        return False, ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT,
                            allow_redirects=True)
        if resp.status_code != 200:
            return False, ""
        final_url = resp.url
        if is_skip(final_url):
            return False, ""

        # Gate: brand word in domain OR the page is the site root (not a profile/directory path)
        final_domain = base_domain(final_url)
        parsed_path = urlparse(final_url).path.strip("/")
        domain_clean = final_domain.replace("-", "").replace(".", "")
        domain_has_brand = any(w in domain_clean for w in bwords)
        is_root_page = not parsed_path or "/" not in parsed_path
        if not domain_has_brand and not is_root_page:
            return False, ""

        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        title = soup.title.string if soup.title else ""
        body = soup.get_text(" ", strip=True)[:5000]
        text = (title + " " + body).lower()
        if any(w in text for w in bwords):
            return True, root_url(final_url)
    except Exception:
        pass
    return False, ""


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
        {"range": f"'{TAB_NAME}'!{col_letter(COL_WEBSITE)}{u['row']}",
         "values": [[u["url"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} websites", flush=True)
    time.sleep(1)


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE) as f:
        return json.load(f)


def cache_urls(cache, company_name):
    key = f'"{company_name}"'
    results = cache.get(key, [])
    return [r.get("url", "") for r in results if r.get("url")]


def apify_google_search(queries):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/apify~google-search-scraper/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"queries": "\n".join(queries), "resultsPerPage": 5,
                  "maxPagesPerQuery": 1, "languageCode": "en",
                  "countryCode": "us", "includeUnfilteredResults": False},
            timeout=300,
        )
    except requests.exceptions.Timeout:
        print("  [!] Apify timeout", flush=True)
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] Apify HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        urls = [r.get("url", "") for r in item.get("organicResults", []) if r.get("url")]
        if q:
            out[q] = urls
    return out


def verify_url_list(urls, company_name):
    """Try each URL in order; return first verified (ok, clean_url) or ('', '')."""
    for url in urls:
        if not url or is_skip(url):
            continue
        ok, clean = html_verify(url, company_name)
        if ok:
            return clean
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", default=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stage", choices=["a", "b", "both"], default="both")
    args = parser.parse_args()

    if args.stage in ("b", "both") and not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    service = get_service()
    print("=== Phase 1: Find Websites (HTML Verification) ===\n", flush=True)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        state = row[COL_STATE] if len(row) > COL_STATE else ""
        city = row[COL_CITY] if len(row) > COL_CITY else ""
        if name.strip() and not website.strip():
            targets.append({
                "row": i + 2,
                "name": name.strip(),
                "state": state.strip(),
                "city": city.strip(),
            })

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need website\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    found_total = 0
    need_stage_b = []

    # ── Stage A: mine classify cache ────────────────────────────────────────
    if args.stage in ("a", "both"):
        print(f"[Stage A] Mining classify cache for {len(targets)} companies...", flush=True)
        cache = load_cache()
        updates = []

        def run_stage_a(t):
            urls = cache_urls(cache, t["name"])
            verified = verify_url_list(urls, t["name"])
            return t, verified

        with ThreadPoolExecutor(max_workers=HTML_WORKERS) as ex:
            futures = {ex.submit(run_stage_a, t): t for t in targets}
            for fut in as_completed(futures):
                t, verified = fut.result()
                if verified:
                    found_total += 1
                    updates.append({"row": t["row"], "url": verified})
                    print(f"  +A {t['name'][:55]:55s} → {verified[:50]}", flush=True)
                else:
                    need_stage_b.append(t)

                if len(updates) >= WRITE_BATCH:
                    flush_updates(service, updates)
                    updates = []

        if updates:
            flush_updates(service, updates)
        print(f"\n  Stage A: found {found_total}, need Stage B: {len(need_stage_b)}\n", flush=True)
    else:
        need_stage_b = targets

    # ── Stage B: fresh Apify search ─────────────────────────────────────────
    if args.stage in ("b", "both") and need_stage_b:
        print(f"[Stage B] Fresh Apify search for {len(need_stage_b)} companies...", flush=True)
        b_found = 0
        queries, qmap = [], {}
        for t in need_stage_b:
            geo = t["city"] or t["state"] or "USA"
            q = f'{t["name"]} {geo} healthcare staffing agency'
            queries.append(q)
            qmap[q] = t

        total_batches = (len(queries) + APIFY_BATCH - 1) // APIFY_BATCH
        updates = []

        for b in range(0, len(queries), APIFY_BATCH):
            batch = queries[b:b + APIFY_BATCH]
            bn = b // APIFY_BATCH + 1
            print(f"  [Apify] Batch {bn}/{total_batches} ({len(batch)} queries)...", flush=True)
            batch_results = apify_google_search(batch)

            # Collect (t, url_list) for parallel HTML verification
            verify_jobs = []
            for q in batch:
                t = qmap.get(q)
                if not t:
                    continue
                urls = batch_results.get(q, [])
                verify_jobs.append((t, urls))

            with ThreadPoolExecutor(max_workers=HTML_WORKERS) as ex:
                futures = {ex.submit(verify_url_list, urls, t["name"]): t
                           for t, urls in verify_jobs}
                for fut in as_completed(futures):
                    t = futures[fut]
                    verified = fut.result()
                    if verified:
                        b_found += 1
                        found_total += 1
                        updates.append({"row": t["row"], "url": verified})
                        print(f"  +B {t['name'][:55]:55s} → {verified[:50]}", flush=True)

                    if len(updates) >= WRITE_BATCH:
                        flush_updates(service, updates)
                        updates = []

            print(f"  Batch {bn} done — Stage B found {b_found} so far", flush=True)

        if updates:
            flush_updates(service, updates)
        print(f"\n  Stage B: found {b_found} / {len(need_stage_b)}\n", flush=True)

    print(f"=== Total websites found: {found_total} / {len(targets)} ===", flush=True)


if __name__ == "__main__":
    main()
