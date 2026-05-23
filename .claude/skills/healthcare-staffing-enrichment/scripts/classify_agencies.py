"""
Phase 0: Classify US healthcare staffing agencies vs non-agencies.

Pipeline:
  1. Read sheet → collect unique Company Names
  2. Batch Google Search via Apify (top 4 organic results per company)
  3. Send snippets to Azure OpenAI GPT-4.1 → healthcare_staffing / not_healthcare_staffing / uncertain
  4. Print report
  5. On --apply: delete not_healthcare_staffing rows; write discovered domain to website col

Cost: ~$0.50 per 200 companies (Apify + GPT-4.1).
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

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_COMPANY_NAME = 3     # D
COL_WEBSITE = 12         # M
COL_CLASSIFICATION = 14  # O (new)
COL_CLS_REASON = 15      # P (new)

SEARCH_BATCH = 50
AI_WORKERS = 8

CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def cache_path():
    return os.path.join(CACHE_DIR, f"classify_search_{SHEET_ID}.json")


def load_cache():
    p = cache_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(data):
    with open(cache_path(), "w") as f:
        json.dump(data, f)


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


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def ensure_classification_headers(service):
    for col_idx, name in [(COL_CLASSIFICATION, "classification"), (COL_CLS_REASON, "classification_reason")]:
        r = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{TAB_NAME}'!{col_letter(col_idx)}1",
        ).execute()
        existing = r.get("values", [[]])[0][0] if r.get("values") else ""
        if not existing:
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"'{TAB_NAME}'!{col_letter(col_idx)}1",
                valueInputOption="RAW",
                body={"values": [[name]]},
            ).execute()


def write_classifications(service, classifications, company_to_rows):
    """Write classification and reason to cols O and P for every row."""
    data = []
    for company, result in classifications.items():
        cls = result.get("classification", "uncertain")
        reason = result.get("reason", "")[:200]
        for row in company_to_rows[company]:
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_CLASSIFICATION)}{row}",
                "values": [[cls]],
            })
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_CLS_REASON)}{row}",
                "values": [[reason]],
            })
    print(f"\n[Writing classifications to sheet] {len(data)//2} rows...")
    for i in range(0, len(data), 500):
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": data[i:i + 500]},
        ).execute()
        print(f"  Wrote chunk {i//500 + 1}/{(len(data)+499)//500}")
    print("  Done.")


def get_tab_sheet_id(service):
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == TAB_NAME:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Tab {TAB_NAME!r} not found")


def apify_google_search(queries):
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
        print(f"  ERROR Apify: {type(e).__name__}: {e}")
        return {}
    if resp.status_code not in (200, 201):
        print(f"  ERROR Apify: HTTP {resp.status_code}: {resp.text[:200]}")
        return {}
    results = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            results[q] = item.get("organicResults", [])
    return results


def extract_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", netloc)
    except Exception:
        return ""


CLASSIFY_SYSTEM = """You classify US companies as healthcare staffing agencies or not.

Classification options:
- healthcare_staffing: The company's PRIMARY business is placing/staffing healthcare workers (nurses, physicians, therapists, allied health, per diem staff, locum tenens, travel nurses) at healthcare facilities. Includes: travel nursing agencies, locum tenens firms, per diem staffing, allied health staffing, medical staffing agencies, therapy staffing, nursing registries.
- not_healthcare_staffing: The company is NOT primarily a healthcare staffing agency. Remove if it is:
  * A direct care provider (hospital, clinic, nursing home, home health agency, hospice, rehab center)
  * A non-healthcare staffing firm (IT, general, administrative, industrial staffing)
  * A technology/software company
  * A consulting firm not primarily staffing healthcare workers
  * A healthcare vendor, supplier, or insurer
  * Any company not primarily in the business of placing healthcare workers at other facilities
- uncertain: Insufficient evidence to classify.

Return ONLY valid JSON:
{"classification": "healthcare_staffing|not_healthcare_staffing|uncertain", "reason": "<one sentence>", "domain": "<primary domain from snippets, or empty string>"}"""

CLASSIFY_USER = """Company name: {company}

Google search snippets (top results):
{snippets}

Classify per the rules. Return JSON only."""


def build_snippet_block(organic):
    lines = []
    for i, r in enumerate(organic[:3], 1):
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
                {"role": "user", "content": CLASSIFY_USER.format(
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", default=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    parser.add_argument("--apply", action="store_true", help="Delete non-agency rows and write domains")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service)
    ensure_classification_headers(service)

    print("=== Classify: Healthcare Staffing Agency vs Not ===")
    print(f"Mode: {'APPLY (will delete rows)' if args.apply else 'DRY RUN'}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A2:Z10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    company_to_rows = {}
    for i, row in enumerate(rows):
        name = row[COL_COMPANY_NAME].strip() if len(row) > COL_COMPANY_NAME and row[COL_COMPANY_NAME] else ""
        if name:
            company_to_rows.setdefault(name, []).append(i + 2)

    companies = sorted(company_to_rows)
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies: {len(companies)}\n")

    print("[1/3] Google Search (with cache)...")
    cache = load_cache()
    cached = sum(1 for c in companies if f'"{c}"' in cache)
    print(f"  Cache: {cached}/{len(companies)} already searched")

    to_search = [c for c in companies if f'"{c}"' not in cache]
    for i in range(0, len(to_search), SEARCH_BATCH):
        batch = to_search[i:i + SEARCH_BATCH]
        queries = [f'"{c}"' for c in batch]
        r = apify_google_search(queries)
        cache.update(r)
        save_cache(cache)
        done = min(i + SEARCH_BATCH, len(to_search))
        print(f"  Searched {done}/{len(to_search)} new ({len(r)} returned)")

    print(f"\n[2/3] Classifying ({AZURE_DEPLOYMENT})...")
    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION,
    )
    classifications = {}

    def run(company):
        organic = cache.get(f'"{company}"', [])
        result = classify_one(client, company, organic)
        return company, result

    with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
        futures = [ex.submit(run, c) for c in companies]
        for i, fut in enumerate(as_completed(futures), 1):
            company, result = fut.result()
            classifications[company] = result
            if i % 25 == 0 or i == len(companies):
                print(f"  Classified {i}/{len(companies)}")

    write_classifications(service, classifications, company_to_rows)

    by_class = {"healthcare_staffing": [], "not_healthcare_staffing": [], "uncertain": []}
    for company, result in classifications.items():
        cls = result.get("classification", "uncertain")
        if cls not in by_class:
            cls = "uncertain"
        by_class[cls].append((company, result))

    print("\n[3/3] Report")
    for cls in ("healthcare_staffing", "not_healthcare_staffing", "uncertain"):
        items = by_class[cls]
        row_count = sum(len(company_to_rows[c]) for c, _ in items)
        print(f"\n=== {cls.upper()}: {len(items)} companies, {row_count} rows ===")
        for company, result in sorted(items, key=lambda x: -len(company_to_rows[x[0]])):
            n = len(company_to_rows[company])
            reason = result.get("reason", "")[:90]
            domain = result.get("domain", "")
            print(f"  {n:4d}  {company!r}  [{domain}]")
            print(f"        → {reason}")

    if not args.apply:
        print("\n[DRY RUN] Re-run with --apply to delete not_healthcare_staffing rows.")
        return

    to_delete = set()
    domain_updates = []
    for company, result in classifications.items():
        cls = result.get("classification", "uncertain")
        sheet_rows = company_to_rows[company]
        if cls == "not_healthcare_staffing":
            to_delete.update(sheet_rows)
        elif cls == "healthcare_staffing":
            domain = (result.get("domain") or "").strip()
            if domain:
                existing_website = ""
                row_data = rows[sheet_rows[0] - 2] if sheet_rows[0] - 2 < len(rows) else []
                existing_website = row_data[COL_WEBSITE] if len(row_data) > COL_WEBSITE else ""
                if not existing_website:
                    for r in sheet_rows:
                        domain_updates.append((r, f"https://{domain}"))

    if domain_updates:
        print(f"\nWriting {len(domain_updates)} domains to website column...")
        col = chr(65 + COL_WEBSITE)
        data = [{"range": f"'{TAB_NAME}'!{col}{row}", "values": [[dom]]} for row, dom in domain_updates]
        for i in range(0, len(data), 500):
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": data[i:i + 500]},
            ).execute()
        print("  Done.")

    if to_delete:
        print(f"\nDeleting {len(to_delete)} not_healthcare_staffing rows...")
        delete_list = sorted(to_delete, reverse=True)
        reqs = [
            {"deleteDimension": {"range": {
                "sheetId": tab_sheet_id, "dimension": "ROWS",
                "startIndex": r - 1, "endIndex": r,
            }}}
            for r in delete_list
        ]
        for i in range(0, len(reqs), 100):
            service.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID, body={"requests": reqs[i:i + 100]},
            ).execute()
            print(f"  Deleted chunk {i // 100 + 1}/{(len(reqs) + 99) // 100}")

        remaining = len(service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!D2:D10000"
        ).execute().get("values", []))
        print(f"\nRows remaining: {remaining}")

    print("=== Done ===")


if __name__ == "__main__":
    main()