"""
Phase 1.75: Classify each unique company as direct_employer / agency / job_board

Pipeline:
  1. Read sheet → collect unique Company Names
  2. Batch Google Search via Apify (top 3 organic results per company)
  3. Send snippets to Claude Haiku → classify + extract primary domain
  4. Print report (companies + classification + reasoning)
  5. On --apply: delete rows where classification ∈ {agency, job_board},
     also write Company Website (col L) for survivors

Why: the Apify Indeed dataset doesn't distinguish recruiters from direct employers.
Cold outreach to agencies / job boards wastes sends — they don't hire, they resell.

Cost: ~$0.50 for 200 companies (Apify $0.002/query + Haiku pennies).
"""

import os
import re
import json
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

TAB_NAME = "Leads"
COL_COMPANY_NAME = 10    # K
COL_COMPANY_WEBSITE = 11 # L

SEARCH_BATCH = 50
CLAUDE_WORKERS = 8

# Persistent cache for Apify Google Search results — keyed by sheet ID so we
# don't re-pay the search cost when classification fails and we need to rerun.
CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def cache_path(spreadsheet_id):
    return os.path.join(CACHE_DIR, f"classify_search_{spreadsheet_id}.json")


def load_search_cache(spreadsheet_id):
    p = cache_path(spreadsheet_id)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def save_search_cache(spreadsheet_id, results):
    with open(cache_path(spreadsheet_id), "w") as f:
        json.dump(results, f)


# --- Google Sheets ---

def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_service():
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


def get_tab_sheet_id(service, spreadsheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Tab {tab_name!r} not found")


# --- Apify Google Search ---

def apify_google_search(queries):
    """Returns dict {query: [organic_results]}. Swallows transient errors —
    timeouts / network errors return {} so the outer loop can continue and
    the cache survives."""
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "queries": "\n".join(queries),
                "resultsPerPage": 4,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "countryCode": "us",
                "includeUnfilteredResults": False,
            },
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"  ERROR from Apify: {type(e).__name__}: {e}")
        return {}
    if resp.status_code not in (200, 201):
        print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:300]}")
        return {}
    try:
        items = resp.json()
    except ValueError:
        print("  ERROR from Apify: invalid JSON")
        return {}
    results = {}
    for item in items:
        query = item.get("searchQuery", {}).get("term", "")
        organic = item.get("organicResults", [])
        if query:
            results[query] = organic
    return results


def extract_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", netloc)
    except Exception:
        return ""


# --- Claude classification ---

CLASSIFY_SYSTEM = """You classify US companies that posted HR-related job openings as one of:

- direct_employer: an actual employer hiring HR / recruiters / benefits / payroll staff for its own workforce. Any industry (tech, healthcare, manufacturing, retail, non-profit, government, etc.) — if they hire people for themselves, they're a direct_employer.
- agency: ANY of the following:
    * Recruitment agency, staffing firm, temp agency, labour supplier
    * Executive search, search firm, headhunter
    * PEO (e.g. Insperity, TriNet, ADP TotalSource, Justworks) — they co-employ workers on behalf of clients
    * RPO (recruitment process outsourcing) provider
    * Fractional HR / HR-as-a-service / outsourced-HR consultancies (they place HR staff at client firms instead of hiring for themselves)
    * HR consulting firms where the primary offering is placing / leasing HR talent to other companies
  If the company's business model is selling HR / recruiting / staffing services to other businesses, it is an agency — DO NOT classify as direct_employer even if they post jobs with their own name.
- job_board: a job aggregator, career site, or job posting platform (Indeed, ZipRecruiter, LinkedIn, Glassdoor, etc.) — not hiring for themselves.
- uncertain: insufficient evidence.

Return ONLY valid JSON of shape:
{"classification": "direct_employer|agency|job_board|uncertain", "reason": "<one short sentence>", "domain": "<primary domain from snippets, or empty>"}

Agency signals in company names: "X Recruiting", "X Recruitment", "X Search", "X Talent", "X Staffing", "X Resources", "X Partners", "X Solutions", "X Consulting Group", "X HR", "Fractional HR X", or a person's name. Agency website signals: "latest jobs", "find talent", "hire with us", "our candidates", "place the right people", "outsourced HR", "HR on demand".
Standalone HR tech SaaS products (BambooHR, Gusto, Rippling, Workday, UKG) are direct_employers — they sell software, not labour.
"""

CLASSIFY_USER_TEMPLATE = """Company name: {company}

Google search snippets (top results for this company name):
{snippets}

Classify per the rules. Return JSON only."""


def build_snippet_block(organic_results):
    lines = []
    for i, r in enumerate(organic_results[:3], 1):
        title = (r.get("title") or "").strip()
        desc = (r.get("description") or r.get("snippet") or "").strip()
        url = (r.get("url") or "").strip()
        lines.append(f"[{i}] {title}\n    {url}\n    {desc}")
    return "\n\n".join(lines) if lines else "(no results)"


def classify_one(client, company, organic):
    snippet_block = build_snippet_block(organic)
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": CLASSIFY_USER_TEMPLATE.format(
                    company=company, snippets=snippet_block,
                )},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"classification": "uncertain", "reason": "no JSON", "domain": ""}
        return json.loads(m.group(0))
    except Exception as e:
        return {"classification": "uncertain", "reason": f"error: {e}", "domain": ""}


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Classify companies as direct employer / agency / job board")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete agency/job_board rows + write domains")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N companies (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    print("=== Classify Companies: Direct Employer vs Agency / Job Board ===")
    print(f"Sheet: {spreadsheet_id}")
    print(f"Mode:  {'APPLY (will delete rows)' if args.apply else 'DRY RUN'}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:AA10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    company_to_rows = {}
    for i, r in enumerate(rows):
        name = r[COL_COMPANY_NAME].strip() if len(r) > COL_COMPANY_NAME and r[COL_COMPANY_NAME] else ""
        if not name:
            continue
        company_to_rows.setdefault(name, []).append(i + 2)

    companies = sorted(company_to_rows.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies: {len(companies)}\n")

    print("[1/3] Running Google Search via Apify (with cache)...")
    results_by_query = load_search_cache(spreadsheet_id)
    cached_hits = sum(1 for c in companies if f'"{c}"' in results_by_query)
    print(f"  Cache: {cached_hits}/{len(companies)} companies already searched")

    to_search = [c for c in companies if f'"{c}"' not in results_by_query]
    for i in range(0, len(to_search), SEARCH_BATCH):
        batch = to_search[i:i + SEARCH_BATCH]
        queries = [f'"{c}"' for c in batch]
        r = apify_google_search(queries)
        results_by_query.update(r)
        save_search_cache(spreadsheet_id, results_by_query)
        print(f"  Searched {min(i + SEARCH_BATCH, len(to_search))}/{len(to_search)} new "
              f"(returned {len(r)} queries)")

    print(f"\n[2/3] Classifying with Azure OpenAI ({AZURE_DEPLOYMENT})...")
    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )
    classifications = {}

    def run(company):
        organic = results_by_query.get(f'"{company}"', [])
        result = classify_one(client, company, organic)
        return company, result, organic

    with ThreadPoolExecutor(max_workers=CLAUDE_WORKERS) as ex:
        futures = [ex.submit(run, c) for c in companies]
        for i, fut in enumerate(as_completed(futures), 1):
            company, result, organic = fut.result()
            classifications[company] = (result, organic)
            if i % 20 == 0 or i == len(companies):
                print(f"  Classified {i}/{len(companies)}")

    by_class = {"direct_employer": [], "agency": [], "job_board": [], "uncertain": []}
    for company, (result, organic) in classifications.items():
        cls = result.get("classification", "uncertain")
        if cls not in by_class:
            cls = "uncertain"
        by_class[cls].append((company, result, organic))

    print("\n[3/3] Report")
    for cls in ("direct_employer", "agency", "job_board", "uncertain"):
        items = by_class[cls]
        row_count = sum(len(company_to_rows[c]) for c, _, _ in items)
        print(f"\n=== {cls.upper()}: {len(items)} companies, {row_count} rows ===")
        for company, result, _ in sorted(items, key=lambda x: -len(company_to_rows[x[0]])):
            n = len(company_to_rows[company])
            reason = result.get("reason", "")[:90]
            domain = result.get("domain", "")
            print(f"  {n:4d}  {company!r}  [{domain}]")
            print(f"        → {reason}")

    if not args.apply:
        print("\n[DRY RUN] No changes made. Re-run with --apply to delete agency/job_board rows.")
        return

    to_delete = set()
    domain_updates = []
    for company, (result, _) in classifications.items():
        cls = result.get("classification", "uncertain")
        sheet_rows = company_to_rows[company]
        if cls in ("agency", "job_board"):
            to_delete.update(sheet_rows)
        elif cls == "direct_employer":
            domain = (result.get("domain") or "").strip()
            if domain:
                for r in sheet_rows:
                    domain_updates.append((r, domain))

    if domain_updates:
        print(f"\nWriting {len(domain_updates)} company domains to column L...")
        data = [
            {"range": f"'{TAB_NAME}'!L{row}", "values": [[dom]]}
            for row, dom in domain_updates
        ]
        for i in range(0, len(data), 500):
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data[i:i + 500]},
            ).execute()
        print("  Domains written.")

    if to_delete:
        print(f"\nDeleting {len(to_delete)} agency/job_board rows (bottom-up)...")
        delete_list = sorted(to_delete, reverse=True)
        requests_body = [
            {"deleteDimension": {"range": {
                "sheetId": tab_sheet_id, "dimension": "ROWS",
                "startIndex": r - 1, "endIndex": r,
            }}}
            for r in delete_list
        ]
        BATCH = 100
        for i in range(0, len(requests_body), BATCH):
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests_body[i:i + BATCH]},
            ).execute()
            print(f"  Deleted chunk {i // BATCH + 1}/{(len(requests_body) + BATCH - 1) // BATCH}")

        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
        ).execute()
        remaining = len(result.get("values", []))
        print(f"\nRows remaining: {remaining}")

    print("=== Done ===")


if __name__ == "__main__":
    main()
