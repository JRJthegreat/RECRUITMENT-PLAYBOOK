"""
Phase 1.9: AI-based clinical role relevance filter.

Indeed's keyword search occasionally returns non-clinical results (admin,
billing, management roles) when a practice description or job listing mentions
clinical keywords. This script uses Azure OpenAI GPT-4.1 to read the job title
and confirm it is a genuine clinical hiring role we can make an introduction for.

KEEP: Family Medicine Physician, Family Practice Physician, Nurse Practitioner
      (any variant: FNP, APRN, NP-C, Family NP), Physician Assistant (PA-C).

DROP: non-clinical roles (Medical Biller, Coder, Medical Assistant, Receptionist,
      Scheduler, Office Manager, Admin, IT), specialty physician roles outside
      current campaign scope (Cardiologist, Neurologist, Dermatologist, etc.),
      or any staffing agency-sourced posting.

Uncertain → DROP (we want a tight, high-quality list).

Dry-run by default. Re-run with --apply to delete DROP rows.
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
COL_JOB_TITLE = 1          # B
COL_COMPANY_NAME = 10      # K
WORKERS = 10


FILTER_SYSTEM = """You decide whether a job TITLE is a clinical role we can make a healthcare staffing introduction for. Judge by title alone — do not infer from company name.

KEEP if the title is one of these in-scope clinical roles:
- Physician (primary care): Family Medicine Physician, Family Practice Physician, Primary Care Physician, Family Doctor, FM Physician, FP Physician
- Nurse Practitioner (any flavor): Nurse Practitioner, NP, FNP, APRN, Family Nurse Practitioner, Family NP, NP-C, Certified Nurse Practitioner
- Physician Assistant (any flavor): Physician Assistant, PA, PA-C, Certified Physician Assistant, PA-C Family Medicine

DROP everything else, including:
- Out-of-scope physician specialties (not current campaign): Cardiologist, Neurologist, Dermatologist, Psychiatrist, OB/GYN, Obstetrician, Gynecologist, Pediatrician, Surgeon, Orthopedic, Radiologist, Anesthesiologist, Ophthalmologist, Urologist, Oncologist, Gastroenterologist, Endocrinologist, Rheumatologist, Pulmonologist
- Non-clinical roles: Medical Assistant, Medical Biller, Medical Coder, CMA, Receptionist, Scheduler, Office Manager, Practice Manager, Practice Administrator, Admin, IT, Healthcare IT, Social Worker, Counselor, Behavioral Health (non-prescriber), Case Manager, Care Coordinator
- Nursing roles that are not NP/APRN: RN, LPN, LVN, Registered Nurse, Licensed Practical Nurse, Staff Nurse, Travel Nurse, Charge Nurse, ICU Nurse, ER Nurse
- Allied health: Physical Therapist, Occupational Therapist, Speech Therapist, Dietitian, Pharmacist, Phlebotomist, Radiologic Technologist, Dental, Optometrist, Chiropractor, Podiatrist

Uncertain → DROP (tight filter — only keep confirmed in-scope clinical titles).

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
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": FILTER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return row_no, {"keep": False, "reason": "no JSON — defaulted DROP"}
        data = json.loads(m.group(0))
        if "keep" not in data:
            return row_no, {"keep": False, "reason": "malformed — defaulted DROP"}
        return row_no, data
    except Exception as e:
        return row_no, {"keep": False, "reason": f"error — defaulted DROP ({e})"}


def main():
    parser = argparse.ArgumentParser(description="AI-based clinical role relevance filter (healthcare pipeline)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete DROP rows")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N rows (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "APPLY (delete DROP rows)" if args.apply else "DRY RUN"
    print(f"=== AI Filter Healthcare Jobs ({mode}) ===")
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
                "keep": data.get("keep", False),
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
