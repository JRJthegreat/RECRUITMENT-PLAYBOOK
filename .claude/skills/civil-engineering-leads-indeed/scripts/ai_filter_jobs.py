"""
Phase 1.9: AI-based job-relevance filter.

Regex filter_relevance.py only catches obvious admin/sales. This script uses
Claude Haiku to read (Job Title + Job Description + Company Name + Company
Description) and decide whether the lead matches our civil-engineering /
construction / infrastructure recruitment criteria.

KEEP: civil engineering, construction, infrastructure (highways, rail,
drainage, structural, bridges), MEP/M&E for buildings, site engineering,
PM/CM on construction projects, industrial engineering services
(pumps, power, fabrication, plant), built-environment consultancies.

DROP: software / IT / DevOps, data / ML, academic research (universities,
institutes), semiconductors / quantum / electronics R&D, medical devices,
aerospace R&D, pure consumer HVAC/appliance service, consumer product design.

Uncertain → KEEP (err on the side of keeping borderline).

Dry-run by default. Re-run with --apply to delete DROP rows.

Cost: ~$0.0004/row with Haiku 4.5 → ~$0.15 per 400 rows.
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


FILTER_SYSTEM = """You decide whether a UK job TITLE is an engineering / technical / hands-on trade / construction / project-delivery role.

Judge the TITLE. Do NOT drop based on the company's industry.

KEEP if the title is any flavour of:
- Engineer (any discipline — civil, structural, software, mechanical, electrical, process, production, manufacturing, HVAC, maintenance, service, field service, installation, commissioning, design, project, site, systems, fire & security, network, telecoms, etc.)
- Developer, Architect (software/IT/building), Technician, Draughtsman, CAD / BIM specialist, Scientist (if hands-on technical)
- Trades: Electrician, Plumber, Welder, Fitter, Fabricator, Mechanic, Carpenter, Joiner, Bricklayer
- ANY project / programme / delivery / contracts management title, regardless of industry. Keep ALL of: Project Manager, Senior Project Manager, Project Lead, Project Delivery Manager, Programme Manager, Delivery Manager, Contracts Manager, Commercial Manager, Construction Manager, Site Manager, Site Supervisor, Foreman, Planner, Estimator, Quantity Surveyor, Sub Agent, Setting-Out Engineer, Installation Supervisor, Scheme Development Lead, Streetworks Coordinator — even if the company is a software / fintech / SaaS / retail / healthcare firm. DO NOT DROP project management titles on industry grounds.
- Health & Safety Advisor / HSE Advisor / SHE Manager
- Utilities Manager, Asset Manager (infrastructure / utilities context)
- Analyst / Specialist IF technical (data analyst, systems analyst, GIS analyst, BIM analyst)

DROP if the title is clearly non-engineering office/support:
- HR / People / Talent / Recruiter / L&D
- Sales / Account / Business Development / Marketing / Brand
- Admin / Receptionist / Coordinator (office) / Executive Assistant / Personal Assistant
- Customer Service / Customer Support / Customer Success / Customer Experience
- Finance / Accountant / Bookkeeper / Payroll / Credit Controller
- Legal / Paralegal / Compliance Officer / Policy
- Teacher / Tutor / Lecturer / Instructor / Trainer (non-engineering)
- Nurse / Carer / Doctor / Healthcare roles
- Lettings / Property Management / Estate Agent
- PMO Analyst / Portfolio Analyst / Business Analyst (pure non-technical office ops)
- Graphic Designer / UX Designer / UI Designer / Brand Designer / Writer / Editor
- Creative / Content / Copywriter / Social Media
- Cleaner / Driver / Warehouse / Labourer / Operative (already stripped upstream but drop if seen)
- Receptionist / Office Manager / Facilities Coordinator
- Research Scientist / Postdoc at universities (academic)
- Buyer / Procurement / Purchasing / Category Manager

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
    parser = argparse.ArgumentParser(description="AI-based job relevance filter")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete DROP rows")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N rows (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "APPLY (delete DROP rows)" if args.apply else "DRY RUN"
    print(f"=== AI Filter Jobs ({mode}) ===")
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
    results = {}  # row_no -> {"keep": bool, "reason": str, "title": str, "company": str}

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
