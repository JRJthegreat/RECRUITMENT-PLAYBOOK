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
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

TAB_NAME = "Leads"
COL_JOB_TITLE = 1          # B
COL_JOB_DESCRIPTION = 9    # J
COL_COMPANY_NAME = 10      # K
COL_COMPANY_DESC = 15      # P
WORKERS = 10
JOB_DESC_CHARS = 800
COMPANY_DESC_CHARS = 400


FILTER_SYSTEM = """You decide whether a US job TITLE is a genuine Human Resources / People / Talent / Benefits / Payroll role.

Judge the TITLE primarily. Use the description only to disambiguate genuinely ambiguous titles.

KEEP if the title is any flavor of:
- HR: HR Manager, HR Director, HR Generalist, HR Business Partner / HRBP, HR Coordinator, HR Administrator, HR Analyst, HR Specialist, HR Consultant, HR Operations, People Operations
- People: Chief People Officer, VP People, Head of People, People Partner, People Ops, People & Culture
- Talent: Talent Acquisition Manager / Director / Partner / Specialist / Coordinator, Recruiter, Senior Recruiter, Technical Recruiter, Executive Recruiter, Sourcer, Talent Sourcer, Talent Operations, Head of Talent
- Senior HR leadership: CHRO, Chief HR Officer, Chief Human Resources Officer, Chief People Officer, VP HR, VP Human Resources, VP of People
- Benefits: Benefits Manager, Benefits Specialist, Benefits Analyst, Benefits Coordinator, Benefits Administrator, Total Rewards, Compensation & Benefits, Comp & Ben
- Compensation: Compensation Analyst / Manager / Director, Total Rewards Manager
- Payroll: Payroll Manager, Payroll Specialist, Payroll Administrator, Payroll Analyst, Payroll Coordinator, Payroll Clerk
- Employee Relations: Employee Relations Manager / Specialist / Partner, ER Partner
- L&D IF clearly people-ops framed: Learning & Development Manager, L&D Partner, Training Manager (HR/people context)
- DEI: Diversity Manager, DEI Director, Head of Inclusion
- Workforce Planning, Workforce Analytics, HRIS Analyst / Manager (HR systems)

DROP if the title is clearly NOT HR/People/Talent/Benefits/Payroll:
- Engineering / trades / technical: Engineer, Technician, Mechanic, Electrician, Plumber, Welder, Driver, Fleet, Operator
- Healthcare / clinical: Nurse, Doctor, Therapist, Caregiver, Medical Assistant
- Sales / BD / marketing (non-HR): Sales Manager, Account Executive, BDR, Marketing Manager, Brand Manager
- Finance / accounting (non-payroll): Accountant, Bookkeeper, Financial Analyst, Controller, AP/AR Specialist
- Operations / production / warehouse: Operations Manager (non-HR), Production Supervisor, Warehouse Manager, Plant Manager
- Customer-facing non-HR: Customer Service Rep, Customer Success Manager, Store Manager, Retail Associate
- Admin / office support that is NOT HR: Receptionist, Office Manager (non-HR), Executive Assistant (non-HR), Administrative Assistant (non-HR)
- Teaching / training that is NOT corporate L&D: Teacher, Tutor, Professor, Instructor, Coach (sports/academic)
- Legal / compliance (non-HR): Paralegal, Attorney, Compliance Officer (non-HR)
- Project Manager (non-HR), Program Manager (non-HR), Product Manager — unless title explicitly says "HR Project Manager" or "People Program Manager"
- Consultant / Advisor that is NOT HR — e.g. Management Consultant, Sales Consultant

Title containing "Manager" alone is NOT enough — it must be HR/People/Talent/Benefits/Payroll context.
"Coordinator" / "Specialist" / "Administrator" / "Analyst" / "Partner" alone without HR context → DROP.

Borderline / ambiguous → KEEP.

Return ONLY valid JSON:
{"keep": true|false, "reason": "<one short sentence referencing the TITLE>"}"""


USER_TEMPLATE = """Company: {company}
Company description: {company_desc}

Job title: {title}
Job description (truncated): {description}

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


def classify_one(client, row_no, title, description, company, company_desc):
    user_msg = USER_TEMPLATE.format(
        company=company or "(unknown)",
        company_desc=(company_desc or "(none)")[:COMPANY_DESC_CHARS],
        title=title or "(unknown)",
        description=(description or "(none)")[:JOB_DESC_CHARS],
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=[{"type": "text", "text": FILTER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text.strip()
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
    print(f"Model: {CLAUDE_MODEL}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:P10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    work = []
    for i, r in enumerate(rows):
        sheet_row = i + 2
        title = r[COL_JOB_TITLE] if len(r) > COL_JOB_TITLE else ""
        description = r[COL_JOB_DESCRIPTION] if len(r) > COL_JOB_DESCRIPTION else ""
        company = r[COL_COMPANY_NAME] if len(r) > COL_COMPANY_NAME else ""
        company_desc = r[COL_COMPANY_DESC] if len(r) > COL_COMPANY_DESC else ""
        if not title.strip():
            continue
        work.append((sheet_row, title, description, company, company_desc))

    if args.limit:
        work = work[:args.limit]
    print(f"Classifying {len(work)} rows with {WORKERS} workers...\n")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    results = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [
            ex.submit(classify_one, client, row_no, title, description, company, company_desc)
            for row_no, title, description, company, company_desc in work
        ]
        meta = {f: (r, t, c) for f, (r, t, _, c, _) in zip(futures, work)}
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
