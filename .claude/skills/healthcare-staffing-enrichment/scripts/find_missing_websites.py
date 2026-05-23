"""
Find websites for companies where col M is blank.

Step A — Companies WITH LinkedIn URL (col G):
  1. Scrape harvestapi/linkedin-company → extract website field
  2. If no website returned → Google fallback (same as Step B)

Step B — Companies WITHOUT LinkedIn URL:
  1. Google "{name} {state} site:linkedin.com/company/" → save LinkedIn URL to col G
  2. If LinkedIn found → scrape harvestapi for website
     If website found from harvestapi → done
     If not → Google fallback
  3. Google fallback: "{name} official website {state}" → GPT-4.1 picks from 5 results

Azure OpenAI 429 errors are handled with exponential backoff (2s → 4s → 8s…, max 5 retries).

Run:
  python3 -W ignore find_missing_websites.py [--limit N]
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
from openai import AzureOpenAI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN     = os.getenv("APIFY_API_TOKEN")
APIFY_BASE      = "https://api.apify.com/v2"
APIFY_GOOGLE    = "apify~google-search-scraper"
APIFY_LINKEDIN  = "harvestapi~linkedin-company"

AZ_CLIENT = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
)
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"

COL_NAME     = 2   # C
COL_LINKEDIN = 5   # F
COL_STATE    = 9   # J
COL_WEBSITE  = 11  # L

APIFY_BATCH  = 50
WRITE_BATCH  = 10
AI_WORKERS   = 3   # low to avoid 429s

SKIP_DOMAINS = {
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "facebook.com", "twitter.com", "instagram.com", "x.com",
    "yelp.com", "bloomberg.com", "crunchbase.com", "zoominfo.com",
    "wikipedia.org", "dnb.com", "bbb.org", "rocketreach.co", "apollo.io",
    "manta.com", "yellowpages.com", "chamberofcommerce.com",
    "opencorporates.com", "buzzfile.com", "dandb.com",
    "highergov.com", "icij.org", "theorg.com", "bebee.com",
    "vivian.com", "nursefly.com", "healthecareers.com", "nursefinders.com",
    "careerbuilder.com", "jobs.com", "zippia.com", "salary.com",
    "google.com", "bing.com", "yahoo.com",
}

STOP_TOKENS = {
    "llc", "inc", "corp", "ltd", "the", "and", "company", "group",
    "healthcare", "health", "medical", "staffing", "solutions", "services",
    "international", "global", "national", "partners",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def base_domain(url):
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\d*\.", "", host)
    except Exception:
        return ""


def root_url(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def is_skip(url):
    d = base_domain(url)
    if d.endswith(".gov") or d.endswith(".edu"):
        return True
    return any(s == d or d.endswith("." + s) for s in SKIP_DOMAINS)


def company_tokens(name):
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in STOP_TOKENS and len(t) >= 3]


def linkedin_slug(url):
    try:
        path = urlparse(url).path
        m = re.search(r"/company/([^/?#]+)", path, re.IGNORECASE)
        return m.group(1).lower() if m else ""
    except Exception:
        return ""


def slug_matches(slug, tokens):
    if not slug or not tokens:
        return False
    slug_chars = re.sub(r"[^a-z0-9]", "", slug)
    return any(t in slug_chars for t in tokens)


def clean_company_name(name):
    return re.sub(r"\s+(llc|inc|corp|ltd|lp|llp)\.?$", "", name.strip(), flags=re.IGNORECASE).strip()


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
    """Each update: {"row": int, "website": str, "linkedin_url": str (optional)}"""
    if not updates:
        return
    data = []
    for u in updates:
        if u.get("website"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_WEBSITE)}{u['row']}",
                "values": [[u["website"]]],
            })
        if u.get("linkedin_url"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN)}{u['row']}",
                "values": [[u["linkedin_url"]]],
            })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    time.sleep(1)


# ── Apify ─────────────────────────────────────────────────────────────────────

def apify_google_search(queries):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_GOOGLE}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "queries": "\n".join(queries),
                "resultsPerPage": 5,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "countryCode": "us",
                "includeUnfilteredResults": False,
            },
            timeout=300,
        )
    except requests.exceptions.Timeout:
        print("  [!] Google search timeout", flush=True)
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] Google HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def scrape_linkedin_for_website(linkedin_url):
    """Call harvestapi/linkedin-company and return the company website URL, or ''."""
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_LINKEDIN}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"companies": [linkedin_url]},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"    [!] harvestapi error: {e}", flush=True)
        return ""
    if resp.status_code not in (200, 201):
        print(f"    [!] harvestapi HTTP {resp.status_code}", flush=True)
        return ""
    items = resp.json()
    if not items:
        return ""
    item = items[0]
    if item.get("error"):
        return ""
    # Try all known field names for company website
    for field in ("website", "websiteUrl", "companyWebsite", "url"):
        val = item.get(field, "")
        if val and isinstance(val, str) and val.startswith("http") and not is_skip(val):
            return root_url(val)
    return ""


def find_linkedin_url_from_results(organic, company_name):
    """Extract validated LinkedIn company URL from Google organic results."""
    tokens = company_tokens(company_name)
    for r in organic:
        url = r.get("url", "") or ""
        if "linkedin.com/company/" not in url.lower():
            continue
        slug = linkedin_slug(url)
        if slug_matches(slug, tokens):
            m = re.search(r"(linkedin\.com/company/[^/?#]+)", url, re.IGNORECASE)
            if m:
                return "https://www." + m.group(1)
    return ""


# ── Azure OpenAI with 429 retry ───────────────────────────────────────────────

def ai_pick_website(company_name, state, results, max_retries=5):
    """Ask GPT-4.1 which result is the company's official website."""
    if not results:
        return ""

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. URL: {r.get('url', '')}\n"
            f"   Title: {r.get('title', '')}\n"
            f"   Description: {r.get('description', '')}"
        )

    prompt = f"""You are identifying the official website of a healthcare staffing company.

Company: {company_name}
State: {state}

Google search results:
{chr(10).join(lines)}

Which result URL is this company's OWN official website?
Rules:
- Must be the company's own website, not a directory, job board, LinkedIn, or government site
- The title or description must clearly match this specific company
- If none clearly match, return empty string

Respond with JSON only: {{"website": "full_url_or_empty_string"}}"""

    delay = 2
    for attempt in range(max_retries):
        try:
            resp = AZ_CLIENT.chat.completions.create(
                model=AZ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=100,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            url = data.get("website", "").strip()
            if url and not is_skip(url):
                return root_url(url)
            return ""
        except Exception as e:
            err = str(e)
            if ("429" in err or "rate" in err.lower()) and attempt < max_retries - 1:
                print(f"  [429] Rate limit — retry in {delay}s (attempt {attempt+1})", flush=True)
                time.sleep(delay)
                delay *= 2
            else:
                if attempt == max_retries - 1:
                    print(f"  [AI error] {e}", flush=True)
                else:
                    print(f"  [AI error] {e}", flush=True)
                return ""
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); return

    service = get_service()

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    # Split targets by whether they have a LinkedIn URL
    has_linkedin = []
    no_linkedin  = []

    for i, row in enumerate(rows):
        name     = row[COL_NAME]     if len(row) > COL_NAME     else ""
        website  = row[COL_WEBSITE]  if len(row) > COL_WEBSITE  else ""
        linkedin = row[COL_LINKEDIN] if len(row) > COL_LINKEDIN else ""
        state    = row[COL_STATE]    if len(row) > COL_STATE    else ""
        if name.strip() and not website.strip():
            t = {"row": i + 2, "name": name.strip(), "state": state.strip()}
            if linkedin.strip():
                t["linkedin_url"] = linkedin.strip()
                has_linkedin.append(t)
            else:
                no_linkedin.append(t)

    if args.limit:
        total = args.limit
        has_linkedin = has_linkedin[:total]
        no_linkedin  = no_linkedin[:max(0, total - len(has_linkedin))]

    total_targets = len(has_linkedin) + len(no_linkedin)
    print(f"=== Find Missing Websites: {total_targets} companies ===", flush=True)
    print(f"  With LinkedIn: {len(has_linkedin)}  |  Without LinkedIn: {len(no_linkedin)}\n", flush=True)

    updates  = []
    found    = 0
    not_found = 0

    # ── Step A: Has LinkedIn → scrape harvestapi → fallback Google ────────────
    if has_linkedin:
        print(f"[Step A] harvestapi scrape — {len(has_linkedin)} companies...\n", flush=True)
        need_google_fallback = []

        for t in has_linkedin:
            website = scrape_linkedin_for_website(t["linkedin_url"])
            if website:
                found += 1
                print(f"  +LI {t['name'][:50]:50s} → {website[:55]}", flush=True)
                updates.append({"row": t["row"], "website": website})
            else:
                print(f"  ~LI {t['name'][:50]:50s} → (no website from LinkedIn)", flush=True)
                need_google_fallback.append(t)

            if len(updates) >= WRITE_BATCH:
                flush(service, updates)
                updates = []

            time.sleep(0.3)

        if updates:
            flush(service, updates)
            updates = []

        # Google fallback for Step A misses
        if need_google_fallback:
            print(f"\n  [Step A fallback] Google+GPT for {len(need_google_fallback)} companies...\n", flush=True)
            queries = [
                f'{t["name"]} healthcare recruitment {t["state"] + " " if t["state"] else ""}US official website'
                for t in need_google_fallback
            ]
            qmap = dict(zip(queries, need_google_fallback))
            total_batches = (len(queries) + APIFY_BATCH - 1) // APIFY_BATCH

            for b in range(0, len(queries), APIFY_BATCH):
                batch = queries[b:b + APIFY_BATCH]
                bn = b // APIFY_BATCH + 1
                print(f"  Google batch {bn}/{total_batches}...", flush=True)
                search_results = apify_google_search(batch)

                def pick_a(q):
                    t = qmap.get(q)
                    if not t:
                        return q, ""
                    results = [
                        {"url": r.get("url", ""), "title": r.get("title", ""), "description": r.get("description", "")}
                        for r in search_results.get(q, []) if r.get("url")
                    ]
                    return q, ai_pick_website(t["name"], t["state"], results)

                with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
                    futures = {ex.submit(pick_a, q): q for q in batch}
                    for fut in as_completed(futures):
                        q, url = fut.result()
                        t = qmap.get(q)
                        if not t:
                            continue
                        if url:
                            found += 1
                            print(f"  +GF {t['name'][:50]:50s} → {url[:55]}", flush=True)
                            updates.append({"row": t["row"], "website": url})
                        else:
                            not_found += 1
                            print(f"  x   {t['name'][:50]:50s} → (not found)", flush=True)

                        if len(updates) >= WRITE_BATCH:
                            flush(service, updates)
                            updates = []

                print(f"  Batch {bn} done — found={found}", flush=True)

            if updates:
                flush(service, updates)
                updates = []

    # ── Step B: No LinkedIn → find LinkedIn + website ─────────────────────────
    if no_linkedin:
        print(f"\n[Step B] No LinkedIn — {len(no_linkedin)} companies\n", flush=True)

        # B1: Search for LinkedIn URL
        li_queries = [
            f'"{clean_company_name(t["name"])}" site:linkedin.com/company/'
            for t in no_linkedin
        ]
        li_qmap = dict(zip(li_queries, no_linkedin))
        total_li_batches = (len(li_queries) + APIFY_BATCH - 1) // APIFY_BATCH

        linkedin_found = {}  # row → linkedin_url

        print(f"  [B1] Google for LinkedIn URLs — {len(no_linkedin)} companies...", flush=True)
        for b in range(0, len(li_queries), APIFY_BATCH):
            batch = li_queries[b:b + APIFY_BATCH]
            bn = b // APIFY_BATCH + 1
            print(f"  LinkedIn batch {bn}/{total_li_batches}...", flush=True)
            batch_results = apify_google_search(batch)

            for q in batch:
                t = li_qmap.get(q)
                if not t:
                    continue
                li_url = find_linkedin_url_from_results(batch_results.get(q, []), t["name"])
                if li_url:
                    linkedin_found[t["row"]] = li_url
                    print(f"  +LI {t['name'][:50]:50s} → {li_url[:60]}", flush=True)
                    # Write LinkedIn URL immediately
                    updates.append({"row": t["row"], "linkedin_url": li_url})

                if len(updates) >= WRITE_BATCH:
                    flush(service, updates)
                    updates = []

        if updates:
            flush(service, updates)
            updates = []

        print(f"\n  LinkedIn found for {len(linkedin_found)}/{len(no_linkedin)} companies", flush=True)

        # B2: For those where we found LinkedIn → scrape harvestapi for website
        need_google_b = []

        if linkedin_found:
            print(f"\n  [B2] harvestapi scrape for {len(linkedin_found)} companies with new LinkedIn...", flush=True)
            li_targets = [t for t in no_linkedin if t["row"] in linkedin_found]

            for t in li_targets:
                li_url = linkedin_found[t["row"]]
                website = scrape_linkedin_for_website(li_url)
                if website:
                    found += 1
                    print(f"  +LI {t['name'][:50]:50s} → {website[:55]}", flush=True)
                    updates.append({"row": t["row"], "website": website})
                else:
                    need_google_b.append(t)

                if len(updates) >= WRITE_BATCH:
                    flush(service, updates)
                    updates = []

                time.sleep(0.3)

            if updates:
                flush(service, updates)
                updates = []

        # Companies with no LinkedIn at all also need Google fallback
        no_li_at_all = [t for t in no_linkedin if t["row"] not in linkedin_found]
        need_google_b.extend(no_li_at_all)

        # B3: Google+GPT for all remaining
        if need_google_b:
            print(f"\n  [B3] Google+GPT for {len(need_google_b)} remaining companies...", flush=True)
            web_queries = [
                f'{t["name"]} healthcare recruitment {t["state"] + " " if t["state"] else ""}US official website'
                for t in need_google_b
            ]
            web_qmap = dict(zip(web_queries, need_google_b))
            total_web_batches = (len(web_queries) + APIFY_BATCH - 1) // APIFY_BATCH

            for b in range(0, len(web_queries), APIFY_BATCH):
                batch = web_queries[b:b + APIFY_BATCH]
                bn = b // APIFY_BATCH + 1
                print(f"  Web batch {bn}/{total_web_batches}...", flush=True)
                search_results = apify_google_search(batch)

                def pick_b(q):
                    t = web_qmap.get(q)
                    if not t:
                        return q, ""
                    results = [
                        {"url": r.get("url", ""), "title": r.get("title", ""), "description": r.get("description", "")}
                        for r in search_results.get(q, []) if r.get("url")
                    ]
                    return q, ai_pick_website(t["name"], t["state"], results)

                with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
                    futures = {ex.submit(pick_b, q): q for q in batch}
                    for fut in as_completed(futures):
                        q, url = fut.result()
                        t = web_qmap.get(q)
                        if not t:
                            continue
                        if url:
                            found += 1
                            print(f"  +GG {t['name'][:50]:50s} → {url[:55]}", flush=True)
                            updates.append({"row": t["row"], "website": url})
                        else:
                            not_found += 1
                            print(f"  x   {t['name'][:50]:50s} → (not found)", flush=True)

                        if len(updates) >= WRITE_BATCH:
                            flush(service, updates)
                            updates = []

                print(f"  Batch {bn} done — found={found}", flush=True)

            if updates:
                flush(service, updates)

    print(f"\n=== Done ===")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"  Total    : {total_targets}")


if __name__ == "__main__":
    main()
