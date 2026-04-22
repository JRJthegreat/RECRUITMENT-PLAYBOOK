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
Cold outreach to agencies/job boards wastes sends — they don't hire, they resell.

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
import anthropic
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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

TAB_NAME = "Leads"
COL_COMPANY_NAME = 10    # K
COL_COMPANY_WEBSITE = 11 # L

SEARCH_BATCH = 50       # Apify queries per call
CLAUDE_WORKERS = 8      # Parallel Haiku classifications


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
    """Returns dict {query: [organic_results]}."""
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={
            "queries": "\n".join(queries),
            "resultsPerPage": 4,
            "maxPagesPerQuery": 1,
            "languageCode": "en",
            "countryCode": "gb",
            "includeUnfilteredResults": False,
        },
        timeout=300,
    )
    if resp.status_code not in (200, 201):
        print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:300]}")
        return {}
    items = resp.json()
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

CLASSIFY_SYSTEM = """You classify UK companies hiring civil engineers as one of:

- direct_employer: an actual civil engineering / construction / infrastructure firm that hires engineers for its own projects (consultancies, contractors, JVs, local authorities, transport operators)
- agency: a recruitment agency, staffing firm, executive search, or labour supplier placing engineers at client firms
- job_board: a job aggregator, professional body job board, or training provider (not hiring for themselves)
- uncertain: insufficient evidence

Return ONLY valid JSON of shape:
{"classification": "direct_employer|agency|job_board|uncertain", "reason": "<one short sentence>", "domain": "<primary domain from snippets, or empty>"}

Civil engineering consultancies (WSP, AECOM, Tony Gee, BWB, Stantec etc.) are direct_employer — they design projects in-house and hire their own engineers.
Recruitment agencies often have names like "X Recruitment", "X Search", "X Talent", "X Resourcing", or a person's name (e.g. "Evan Craig"). Their websites advertise "latest jobs" lists.
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
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=[{"type": "text", "text": CLASSIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": CLASSIFY_USER_TEMPLATE.format(
                company=company, snippets=snippet_block,
            )}],
        )
        text = resp.content[0].text.strip()
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

    # 1. Read sheet, collect unique companies → list of (company, [sheet_rows])
    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:AC10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    company_to_rows = {}
    for i, r in enumerate(rows):
        name = r[COL_COMPANY_NAME].strip() if len(r) > COL_COMPANY_NAME and r[COL_COMPANY_NAME] else ""
        if not name:
            continue
        company_to_rows.setdefault(name, []).append(i + 2)  # sheet row number

    companies = sorted(company_to_rows.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies: {len(companies)}\n")

    # 2. Batch Google Search
    print("[1/3] Running Google Search via Apify...")
    results_by_query = {}
    for i in range(0, len(companies), SEARCH_BATCH):
        batch = companies[i:i + SEARCH_BATCH]
        queries = [f'"{c}"' for c in batch]
        r = apify_google_search(queries)
        results_by_query.update(r)
        print(f"  Searched {min(i + SEARCH_BATCH, len(companies))}/{len(companies)} "
              f"(returned {len(r)} queries)")

    # 3. Classify via Claude (parallel)
    print(f"\n[2/3] Classifying with {CLAUDE_MODEL}...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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

    # 4. Report
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

    # 5. Apply: delete agency/job_board rows + write domain for direct_employer
    if not args.apply:
        print("\n[DRY RUN] No changes made. Re-run with --apply to delete agency/job_board rows.")
        return

    to_delete = set()
    domain_updates = []  # [(sheet_row, domain)]
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

    # Write domains first (before deletion shifts rows)
    if domain_updates:
        print(f"\nWriting {len(domain_updates)} company domains to column L...")
        data = [
            {"range": f"'{TAB_NAME}'!L{row}", "values": [[dom]]}
            for row, dom in domain_updates
        ]
        # Batch in chunks of 500
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

        # Verify
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
        ).execute()
        remaining = len(result.get("values", []))
        print(f"\nRows remaining: {remaining}")

    print("=== Done ===")


if __name__ == "__main__":
    main()
