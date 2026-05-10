"""
Phase 1.9: AI-based HR-role relevance filter.

Indeed's keyword search is broad — "HR Manager" etc. can pull in unrelated
roles (mechanics, drivers, sales) when a company description or job listing
happens to mention HR-adjacent terms. This script uses Claude Haiku to read
(Job Title + Job Description + Company Name + Company Description) and
decide whether the title is genuinely an HR / Talent / People / Benefits /
Payroll role.

KEEP: HR, People, Talent, Recruiting/Recruitment, Benefits, Compensation,
Payroll, Employee Relations, HRBP, CHRO, Chief People/HR, L&D (if people-ops),
Workforce Planning, DEI, Total Rewards.

DROP: anything not actually HR/People/Talent/Benefits/Payroll — e.g.
mechanic, driver, fleet, sales, engineer, nurse, teacher, operations
manager (non-HR), admin assistant (non-HR), warehouse, production, etc.

Uncertain → KEEP (err on the side of keeping borderline).

Dry-run by default. Re-run with --apply to delete DROP rows.

Cost: ~$0.0004/row with Haiku 4.5 → ~$0.20 per 500 rows.
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
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.1")

TAB_NAME = "Leads"
COL_JOB_TITLE = 1          # B
COL_COMPANY_NAME = 10      # K
WORKERS = 10


FILTER_SYSTEM = """You decide whether a job TITLE is a genuine HR / People / Talent / Recruiting / Benefits / Payroll role. Judge by title alone — do not infer from company name.

KEEP if the title is clearly one of these:
- HR generalist/specialist: HR Manager, HR Director, HR Generalist, HR Business Partner, HRBP, HR Coordinator, HR Administrator, HR Analyst, HR Specialist, HR Consultant, HR Operations, People Operations, HR Assistant
- People & Culture: Chief People Officer, VP People, Head of People, People Partner, People Ops, People & Culture Manager/Director
- Talent / Recruiting: Recruiter (any flavor — Technical, Executive, Senior, Campus, etc.), Talent Acquisition Manager/Director/Partner/Specialist/Coordinator, Sourcer, Talent Sourcer, Head of Talent, Talent Operations
- Senior HR leadership: CHRO, Chief HR Officer, Chief Human Resources Officer, VP HR, VP Human Resources, VP of People, SVP HR
- Benefits / Compensation: Benefits Manager/Specialist/Analyst/Coordinator/Administrator, Total Rewards Manager/Analyst, Compensation Manager/Analyst/Director, Comp & Benefits
- Payroll: Payroll Manager, Payroll Specialist, Payroll Administrator, Payroll Analyst, Payroll Coordinator, Payroll Clerk
- Employee Relations: Employee Relations Manager/Specialist/Partner, ER Partner
- L&D (people-ops context only): Learning & Development Manager, L&D Partner, Training Manager/Coordinator (HR/people context)
- DEI: Diversity Manager, DEI Director, Head of Inclusion, Chief Diversity Officer
- HR systems/data: Workforce Planning Analyst, Workforce Analytics, HRIS Analyst/Manager

DROP everything that is NOT a dedicated HR/People/Talent/Recruiting/Benefits/Payroll role:
- ANY management title that is not explicitly HR/People/Talent: Store Manager, Office Manager, General Manager, Operations Manager, Regional Manager, District Manager, Branch Manager, Area Manager, Practice Manager, Facilities Manager — DROP these even if the company is an HR-adjacent industry
- Clinical / healthcare: Doctor, Dentist, Orthodontist, Nurse, Therapist, Physician, Surgeon, Medical Assistant, Caregiver, Hygienist, Optometrist
- Food / hospitality: Chef, Sous Chef, Cook, Kitchen Staff, Server, Bartender, Dishwasher
- Trades / technical: Driver, Mechanic, Technician, Electrician, Plumber, Welder, Operator, Installer, Laborer
- Sales / marketing: Account Executive, BDR, Sales Rep, Sales Manager, Marketing Manager, Brand Manager
- Finance / accounting (non-payroll): Accountant, Bookkeeper, Financial Analyst, Controller, CFO, AP/AR Specialist
- Teaching: Teacher, Tutor, Professor, Instructor, Coach
- Legal: Paralegal, Attorney, Lawyer
- Admin/support (non-HR): Receptionist, Administrative Assistant, Executive Assistant, Customer Service Rep, Retail Associate, Warehouse Worker

A title with "Manager" is only KEEP if HR/People/Talent/Recruiting/Benefits/Payroll appears in it (e.g. "HR Manager" = KEEP, "Store Manager" = DROP, "Benefits Manager" = KEEP, "Office Manager" = DROP).

Uncertain → DROP (we want a tight, high-quality HR list).

Return ONLY valid JSON:
{"keep": true|false, "reason": "<one short sentence referencing the TITLE>"}"""


USER_TEMPLATE = """Company: {company}
Job title: {title}

Classify per the rules. Return JSON only."""


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


def classify_one(client, row_no, title, company):
    user_msg = USER_TEMPLATE.format(
        company=company or "(unknown)",
        title=title or "(unknown)",
    )
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_completion_tokens=200,
            temperature=1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": FILTER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return row_no, {"keep": True, "reason": "no JSON — defaulted KEEP"}
        data = json.loads(m.group(0))
        if "keep" not in data:
            return row_no, {"keep": True, "reason": "malformed — defaulted KEEP"}
        return row_no, data
    except Exception as e:
        return row_no, {"keep": True, "reason": f"error — defaulted KEEP ({e})"}


def main():
    parser = argparse.ArgumentParser(description="AI-based HR-role relevance filter")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete DROP rows")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N rows (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "APPLY (delete DROP rows)" if args.apply else "DRY RUN"
    print(f"=== AI Filter HR Jobs ({mode}) ===")
    print(f"Model: {AZURE_DEPLOYMENT}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:K10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    work = []
    for i, r in enumerate(rows):
        sheet_row = i + 2
        title = r[COL_JOB_TITLE] if len(r) > COL_JOB_TITLE else ""
        company = r[COL_COMPANY_NAME] if len(r) > COL_COMPANY_NAME else ""
        if not title.strip():
            continue
        work.append((sheet_row, title, company))

    if args.limit:
        work = work[:args.limit]
    print(f"Classifying {len(work)} rows with {WORKERS} workers...\n")

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )
    results = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [
            ex.submit(classify_one, client, row_no, title, company)
            for row_no, title, company in work
        ]
        meta = {f: (r, t, c) for f, (r, t, c) in zip(futures, work)}
        done = 0
        for fut in as_completed(futures):
            row_no, data = fut.result()
            orig_row_no, title, company = meta[fut]
            results[orig_row_no] = {
                "keep": data.get("keep", True),
                "reason": data.get("reason", ""),
                "title": title,
                "company": company,
            }
            done += 1
            if done % 25 == 0 or done == len(futures):
                print(f"  Classified {done}/{len(futures)}")

    keeps = [(r, d) for r, d in results.items() if d["keep"]]
    drops = [(r, d) for r, d in results.items() if not d["keep"]]

    print(f"\n=== KEEP: {len(keeps)} ===")
    print(f"=== DROP: {len(drops)} ===\n")

    if drops:
        print("Drops (grouped by company):")
        from collections import defaultdict
        by_company = defaultdict(list)
        for r, d in drops:
            by_company[d["company"]].append((r, d))
        for company in sorted(by_company.keys()):
            items = by_company[company]
            print(f"\n  {company}  ({len(items)} row{'s' if len(items) > 1 else ''})")
            for r, d in items[:5]:
                print(f"    row {r}: {d['title']!r}")
                print(f"            → {d['reason'][:150]}")
            if len(items) > 5:
                print(f"    ... and {len(items) - 5} more")

    if not args.apply:
        print("\n[DRY RUN] No changes made. Re-run with --apply to delete DROP rows.")
        return

    to_delete = sorted([r for r, _ in drops], reverse=True)
    if not to_delete:
        print("\nNothing to delete.")
        return

    print(f"\nDeleting {len(to_delete)} DROP rows (bottom-up)...")
    reqs = [
        {"deleteDimension": {"range": {
            "sheetId": tab_sheet_id, "dimension": "ROWS",
            "startIndex": r - 1, "endIndex": r,
        }}}
        for r in to_delete
    ]
    BATCH = 100
    for i in range(0, len(reqs), BATCH):
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": reqs[i:i + BATCH]},
        ).execute()
        print(f"  Deleted chunk {i // BATCH + 1}/{(len(reqs) + BATCH - 1) // BATCH}")

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
    ).execute()
    print(f"\nRows remaining: {len(result.get('values', []))}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
