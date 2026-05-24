"""
Phase 1.75: Classify each unique company as direct_employer or a DROP category.

Pipeline:
  1. Read sheet → collect unique companies with job + company description
  2. Send to Azure OpenAI GPT-4.1 → classify
  3. Print report (companies + classification + reasoning)
  4. On --apply: delete rows where classification != direct_employer

Healthcare-specific DROP categories:
  - hospital_system: large hospital systems, health systems, academic medical centers
  - chain: multi-location urgent care or PE-backed practice groups
  - fqhc_government: FQHCs, VA clinics, federally-funded health centers, government
  - agency: healthcare staffing firms posting physician jobs to recruit candidates
  - uncertain: anything ambiguous → DROP (tight quality filter)
"""

import os
import re
import json
import argparse
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

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

TAB_NAME = "Leads"
COL_JOB_TITLE = 1      # B
COL_JOB_DESC = 9       # J
COL_COMPANY_NAME = 10  # K
COL_COMPANY_DESC = 15  # P

LLM_WORKERS = 8

DROP_CATEGORIES = {"hospital_system", "chain", "fqhc_government", "agency", "uncertain"}


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


# --- LLM classification ---

CLASSIFY_SYSTEM = """You classify US healthcare employers that posted clinical job openings (Family Medicine, Nurse Practitioner, Physician Assistant, etc.) into one of these categories:

- direct_employer: A small private practice, physician-owned clinic, or small medical group (typically ≤ 10 locations) that is hiring clinical staff for themselves. These are independent practices without large internal HR or talent acquisition departments. Examples: "Lake Shore Family Medicine", "Dr. Rodriguez & Associates", "Westchester Primary Care PLLC", "Capital Women's Health".

- hospital_system: A hospital, health system, academic medical center, or large regional healthcare organization. Signals: "Medical Center", "Health System", "University Hospital", "Regional Medical", "Children's Hospital", "Memorial Hospital", "Memorial Health", "NYU Langone", "Johns Hopkins", "Kaiser Permanente", "Mount Sinai", "Northwell", "UPMC".

- chain: A multi-location, corporate-owned, or private equity-backed healthcare chain. Examples: CityMD, AFC Urgent Care, GoHealth Urgent Care, Carbon Health, One Medical, Oak Street Health, VillageMD, Privia Health, Agilon Health, Optum, MinuteClinic, CVS Health, Walgreens Health, DispatchHealth, Concentra, TeamHealth.

- fqhc_government: A Federally Qualified Health Center (FQHC), community health center with federal funding, VA clinic, Veterans Affairs facility, or any other government-run health facility. Signals: "Community Health Center", "FQHC", "Federally Qualified", "Veterans Affairs", "VA Clinic", "Indian Health Service", "Public Health Service", "Department of Health".

- agency: A healthcare staffing agency, locum tenens agency, or travel nursing company posting physician/NP/PA jobs to recruit candidates onto their own roster (not because a clinic is hiring). Signals: "Staffing", "Locums", "Locum Tenens", "Medical Staffing", "Travel Nursing", "on behalf of our client", "we are placing", company name includes "Staffing" / "Locums" / "Recruiting" / "Healthcare Solutions".

- uncertain: Insufficient evidence to classify confidently.

Return ONLY valid JSON: {"classification": "direct_employer|hospital_system|chain|fqhc_government|agency|uncertain", "reason": "<one short sentence>"}

When in doubt, classify as uncertain — we only want confirmed small private practices.
"""

CLASSIFY_USER_TEMPLATE = """Company name: {company}
Job title: {job_title}
{company_desc_block}Job description (first 800 chars):
{job_desc}

Classify per the rules. Return JSON only."""


def classify_one(client, company, job_title, job_desc, company_desc):
    company_desc_block = f"Company description: {company_desc}\n" if company_desc else ""
    job_desc_truncated = job_desc[:800] if job_desc else "(not available)"
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": CLASSIFY_USER_TEMPLATE.format(
                    company=company,
                    job_title=job_title,
                    company_desc_block=company_desc_block,
                    job_desc=job_desc_truncated,
                )},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"classification": "uncertain", "reason": "no JSON"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"classification": "uncertain", "reason": f"error: {e}"}


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Classify healthcare employers (private practice vs hospital/chain/agency)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete non-direct_employer rows")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N companies (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    print("=== Classify Healthcare Companies ===")
    print(f"Sheet: {spreadsheet_id}")
    print(f"Model: {AZURE_DEPLOYMENT}")
    print(f"Mode:  {'APPLY (will delete rows)' if args.apply else 'DRY RUN'}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:AA10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    def safe_get(row, idx):
        return row[idx].strip() if len(row) > idx and row[idx] else ""

    company_to_rows = {}
    company_data = {}
    for i, r in enumerate(rows):
        name = safe_get(r, COL_COMPANY_NAME)
        if not name:
            continue
        company_to_rows.setdefault(name, []).append(i + 2)
        if name not in company_data:
            company_data[name] = {
                "job_title": safe_get(r, COL_JOB_TITLE),
                "job_desc": safe_get(r, COL_JOB_DESC),
                "company_desc": safe_get(r, COL_COMPANY_DESC),
            }

    companies = sorted(company_to_rows.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies: {len(companies)}\n")

    print(f"Classifying {len(companies)} companies with {AZURE_DEPLOYMENT}...")
    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )
    classifications = {}

    def run(company):
        data = company_data[company]
        result = classify_one(
            client, company,
            data["job_title"], data["job_desc"], data["company_desc"],
        )
        return company, result

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as ex:
        futures = [ex.submit(run, c) for c in companies]
        for i, fut in enumerate(as_completed(futures), 1):
            company, result = fut.result()
            classifications[company] = result
            if i % 20 == 0 or i == len(companies):
                print(f"  Classified {i}/{len(companies)}")

    all_categories = ["direct_employer", "hospital_system", "chain", "fqhc_government", "agency", "uncertain"]
    by_class = {cat: [] for cat in all_categories}
    for company, result in classifications.items():
        cls = result.get("classification", "uncertain")
        if cls not in by_class:
            cls = "uncertain"
        by_class[cls].append((company, result))

    print("\n=== Report ===")
    for cls in all_categories:
        items = by_class[cls]
        row_count = sum(len(company_to_rows[c]) for c, _ in items)
        label = "KEEP" if cls == "direct_employer" else "DROP"
        print(f"\n{cls.upper()} [{label}]: {len(items)} companies, {row_count} rows")
        for company, result in sorted(items, key=lambda x: -len(company_to_rows[x[0]])):
            n = len(company_to_rows[company])
            reason = result.get("reason", "")[:90]
            print(f"  {n:4d}  {company!r}")
            print(f"        → {reason}")

    if not args.apply:
        print("\n[DRY RUN] No changes made. Re-run with --apply to delete non-direct_employer rows.")
        return

    to_delete = set()
    for company, result in classifications.items():
        cls = result.get("classification", "uncertain")
        if cls in DROP_CATEGORIES:
            to_delete.update(company_to_rows[company])

    if to_delete:
        print(f"\nDeleting {len(to_delete)} non-direct_employer rows (bottom-up)...")
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

        remaining = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
        ).execute()
        print(f"\nRows remaining: {len(remaining.get('values', []))}")
    else:
        print("\nNo rows to delete.")

    print("=== Done ===")


if __name__ == "__main__":
    main()
