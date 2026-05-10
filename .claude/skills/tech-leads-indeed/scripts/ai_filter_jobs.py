"""
Phase 1.9: AI-based job-relevance filter (tech).

Indeed datasets surface non-engineering roles that slip past keyword scrapes
(e.g. "Engineering Manager" returns operations/manufacturing roles, "DevOps"
hits sales/BD reps at DevOps tooling vendors). This script uses Azure OpenAI
GPT-4.1 to read (Job Title + Job Description + Company Name + Company
Description) and decide whether the role is a placeable engineering / IT /
technical IC position.

KEEP: software / backend / frontend / full-stack engineers, DevOps / SRE /
platform / cloud / infrastructure, data / ML / AI engineers, mobile (iOS/
Android), QA / test automation, embedded / firmware, security engineers,
engineering managers, tech leads, software / solution / cloud architects,
forward-deployed / solutions engineers (technical post-sale), staff /
principal / distinguished engineers.

DROP: sales / account exec / BDR, marketing / brand / growth, HR / People /
Talent / recruiters, customer success / support / experience, non-technical
project / programme / portfolio managers, BAs / PMO analysts, designers
(UX/UI/Graphic/Brand), product managers (we place engineers, not PMs),
finance / accounting / legal, operations managers (non-engineering),
office / admin / executive assistant, content / copywriter / social media,
warehouse / driver / labourer, teachers / nurses / healthcare, academic
researchers, estate agents.

Uncertain → KEEP (err on the side of keeping borderline engineering roles).

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
COL_JOB_DESCRIPTION = 9    # J
COL_COMPANY_NAME = 10      # K
COL_COMPANY_DESC = 15      # P
WORKERS = 10
JOB_DESC_CHARS = 800
COMPANY_DESC_CHARS = 400


FILTER_SYSTEM = """You decide whether a TITLE is a placeable engineering / IT / technical IC role for a pan-European tech recruitment agency.

Judge primarily on the TITLE. Use the description only to disambiguate borderline titles. Do NOT drop based on the company's industry — a Backend Engineer at a fintech is just as valid as one at a logistics firm.

KEEP if the title is any of:
- Software / Backend / Frontend / Full-Stack / Mobile / iOS / Android Engineer or Developer (any seniority: Junior, Mid, Senior, Staff, Principal, Distinguished, Lead)
- DevOps, SRE, Platform, Infrastructure, Cloud, Site Reliability, Production, Build/Release Engineer
- Data Engineer, Analytics Engineer, ML / AI / Machine Learning / Deep Learning Engineer, MLOps, Data Scientist (when hands-on engineering, not pure research)
- QA / Test / Test Automation / SDET / Quality Engineer
- Embedded / Firmware / Hardware / Robotics / Systems / Kernel Engineer
- Security / AppSec / InfoSec / DevSecOps / Cybersecurity Engineer
- Software / Solution / Cloud / Data / Enterprise / Technical Architect
- Engineering Manager, Eng Manager, Tech Lead, Team Lead, Group Lead (engineering — mid-level management is fine)
- Forward-Deployed / Solutions / Sales Engineer (technical post-sale, hands-on with the product)
- Site Reliability Manager, Platform Lead, Staff+ engineering ICs of any flavour
- Specialist technical roles: Database Engineer, Network Engineer, Systems Administrator, SysOps, Reliability Engineer, Observability Engineer, Performance Engineer, Quant Developer
- Product Manager / Senior Product Manager / Group Product Manager / Product Lead / Product Owner — tech firms hire PMs as part of the engineering function. KEEP unless the title is clearly non-tech (e.g. "Beauty Product Manager", "Brand Product Manager")

DROP C-suite and executive leadership — these are exec-search roles, not placeable through standard tech recruitment:
- CTO / Chief Technology Officer / Chief Tech Officer
- CEO / Chief Executive Officer
- COO / Chief Operating Officer
- CFO / Chief Financial Officer
- CPO / Chief Product Officer
- VP Engineering / VP of Engineering / Vice President Engineering
- Head of Engineering / Head of Software Engineering / Head of Technology
- Director of Engineering / Director of Software Engineering / Director of Technology
- Chief Architect / Principal Architect (when clearly exec-level, not IC)

DROP if the title is clearly non-engineering office / commercial / non-technical:
- Sales / Account Executive / BDR / SDR / Business Development / Partnerships
- Marketing / Brand / Growth / SEO / Content / Copywriter / Social Media
- HR / People / Talent / Recruiter / L&D / Talent Acquisition
- Customer Success / Customer Support / Customer Experience / CSM
- Finance / Accountant / FP&A / Controller / Bookkeeper / Payroll
- Legal / Compliance / Paralegal / Policy
- Operations Manager / Office Manager / Facilities (non-engineering ops)
- Project Manager / Programme Manager / Delivery Manager (when clearly non-technical — e.g. marketing PM, HR PM, business PM)
- Designer (UX, UI, Graphic, Brand, Product Designer, Web Designer)
- Business Analyst / PMO Analyst / Portfolio Analyst (pure office)
- Researcher / Postdoc / Lecturer / Academic Scientist
- Driver / Warehouse / Labourer / Operative / Cleaner
- Teacher / Tutor / Trainer / Coach (non-technical)
- Nurse / Carer / Doctor / Healthcare workers
- Estate Agent / Lettings / Property Manager
- Receptionist / Executive Assistant / Personal Assistant / Admin
- Buyer / Procurement / Purchasing / Supply Chain Coordinator

Borderline rules:
- Project Manager / Delivery Manager IN A TECH/SOFTWARE CONTEXT → DROP (we place engineers, not delivery managers).
- Technical Project Manager / Engineering Project Manager → DROP (still PM, not IC).
- Data Analyst → DROP (analytics seat, not engineering). Data Engineer → KEEP.
- BI Developer → KEEP (technical IC, builds pipelines/dashboards).
- Scrum Master / Agile Coach → DROP.
- IT Support / Help Desk / Service Desk → DROP (operational, not engineering).
- Network / Systems Engineer (corporate IT) → KEEP (technical, hands-on).
- Solutions Engineer / Sales Engineer → KEEP (technical post-sale).
- Account Engineer → DROP (commercial-leaning).
- Truly ambiguous → KEEP.

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
    import time as _time
    user_msg = USER_TEMPLATE.format(
        company=company or "(unknown)",
        company_desc=(company_desc or "(none)")[:COMPANY_DESC_CHARS],
        title=title or "(unknown)",
        description=(description or "(none)")[:JOB_DESC_CHARS],
    )
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=AZURE_DEPLOYMENT,
                max_tokens=200,
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
                return row_no, {"keep": False, "reason": "no JSON — DROP"}
            data = json.loads(m.group(0))
            if "keep" not in data:
                return row_no, {"keep": False, "reason": "malformed — DROP"}
            return row_no, data
        except Exception as e:
            err = str(e)
            if ("429" in err or "Too Many Requests" in err) and attempt < 3:
                _time.sleep(8 * (2 ** attempt))  # 8s, 16s, 32s
                continue
            return row_no, {"keep": False, "reason": f"failed after retries — DROP ({err[:80]})"}


def main():
    parser = argparse.ArgumentParser(description="AI-based tech job relevance filter")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete DROP rows")
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N rows (debug)")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "APPLY (delete DROP rows)" if args.apply else "DRY RUN"
    print(f"=== AI Filter Jobs — Tech ({mode}) ===")
    print(f"Model: Azure OpenAI {AZURE_DEPLOYMENT}\n")

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

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )
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
