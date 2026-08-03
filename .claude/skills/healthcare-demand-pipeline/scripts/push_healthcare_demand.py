"""
Phase 5 — push generated healthcare demand leads to the Instantly campaign
"Healthcare Clinical Demand — Midwest" (f59f4fa5-ec7d-45b6-92b4-4ec6417e66cd).

Eligible rows: dm_status (AB) starts with 'found', email (W) + body (Z) present,
AA not TRUE/BLOCKLISTED. One lead per POST (no bulk endpoint).

Protections (same conventions as push_demand_campaign.py):
  - duplicate emails skipped (within run + rows already AA=TRUE anywhere)
  - email root-domain must match website root-domain (mismatches logged,
    skipped, left unmarked for review — not blocked forever)
  - workspace-blocklist 400s -> AA=BLOCKLISTED, never retried
  - AA=TRUE written in batches of 10 (crash-safe resume)

Custom variables MUST be sent as "custom_variables" (Instantly merges them
into the stored payload). Sending them nested under "payload" gets silently
replaced; loose top-level keys are dropped.

Run:
  python3 -W ignore push_healthcare_demand.py --sheet_url "URL" [--limit N] [--dry_run]
"""

import os
import re
import time
import argparse
import requests
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

INSTANTLY_KEY = os.getenv("INSTANTLY_API_KEY")
BASE = "https://api.instantly.ai/api/v2"


TAB = "Leads"
BATCH = 10

# 0-based cols
C_JOBID, C_TITLE, C_COMPANY, C_WEBSITE = 0, 1, 10, 11
C_CITY = 17
C_DM_TITLE, C_EMAIL, C_FIRST, C_LAST = 20, 22, 23, 24
C_BODY, C_ADDED, C_STATUS = 25, 26, 27
C_CROLE, C_RPLUR, C_TEAM, C_ETYPE, C_MONTH, C_CCOMP = 31, 32, 33, 34, 35, 36
C_CFIRST, C_CCITY = 38, 39


def headers():
    return {"Authorization": f"Bearer {INSTANTLY_KEY}",
            "Content-Type": "application/json"}


def root_domain(host):
    host = (host or "").lower().strip()
    if not host:
        return ""
    if host.startswith("http"):
        host = urlparse(host).netloc
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--campaign_id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url)
    sheet_id = m.group(1)
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    svc = build("sheets", "v4", credentials=creds)
    rows = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB}!A1:AN").execute().get("values", [])
    data = rows[1:]

    def cell(r, i):
        return r[i].strip() if i < len(r) and r[i] else ""

    seen = {cell(r, C_EMAIL).lower() for r in data
            if cell(r, C_ADDED).upper() == "TRUE" and cell(r, C_EMAIL)}

    todo, skipped_dupe, skipped_domain = [], 0, []
    for i, r in enumerate(data):
        if args.limit and len(todo) >= args.limit:
            break
        if not cell(r, C_STATUS).startswith("found"):
            continue
        if cell(r, C_ADDED).upper() in ("TRUE", "BLOCKLISTED"):
            continue
        email, body = cell(r, C_EMAIL), cell(r, C_BODY)
        if not email or not body:
            continue
        if email.lower() in seen:
            skipped_dupe += 1
            continue
        wd, ed = root_domain(cell(r, C_WEBSITE)), root_domain(email.split("@")[-1])
        if wd and ed and wd != ed:
            skipped_domain.append((i + 2, cell(r, C_COMPANY), email))
            continue
        seen.add(email.lower())
        todo.append((i + 2, r))

    print(f"to push: {len(todo)} | dupes skipped: {skipped_dupe} | "
          f"domain-mismatch held for review: {len(skipped_domain)}")
    for row_n, comp, em in skipped_domain[:15]:
        print(f"  HELD row {row_n}: {comp} | {em}")
    if args.dry_run:
        return

    pushed, blocklisted, failed = 0, 0, []
    pending = []

    def flush():
        nonlocal pending
        if pending:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": pending}).execute()
            pending = []

    for n, (row_n, r) in enumerate(todo, 1):
        job_id = cell(r, C_JOBID)
        payload = {
            "campaign": args.campaign_id,
            "email": cell(r, C_EMAIL),
            "first_name": cell(r, C_CFIRST) or cell(r, C_FIRST),
            "last_name": cell(r, C_LAST),
            "company_name": cell(r, C_CCOMP) or cell(r, C_COMPANY),
            "website": cell(r, C_WEBSITE),
            "personalization": cell(r, C_BODY),
            "custom_variables": {
                "cleaned_role": cell(r, C_CROLE),
                "role_plural": cell(r, C_RPLUR),
                "team_word": cell(r, C_TEAM),
                "employer_type": cell(r, C_ETYPE),
                "month": cell(r, C_MONTH),
                "casual_company": cell(r, C_CCOMP),
                "city": cell(r, C_CCITY) or cell(r, C_CITY),
                "job_title": cell(r, C_TITLE),
                "dm_title": cell(r, C_DM_TITLE),
                "job_post_url": f"https://indeed.com/viewjob?jk={job_id}" if job_id else "",
                "company_website": cell(r, C_WEBSITE),
            },
        }
        try:
            resp = requests.post(f"{BASE}/leads", headers=headers(),
                                 json=payload, timeout=30)
        except requests.RequestException as e:
            failed.append((row_n, str(e)[:80]))
            continue
        if resp.status_code == 200:
            pending.append({"range": f"{TAB}!AA{row_n}", "values": [["TRUE"]]})
            pushed += 1
        elif resp.status_code == 400 and "blocklist" in resp.text.lower():
            pending.append({"range": f"{TAB}!AA{row_n}", "values": [["BLOCKLISTED"]]})
            blocklisted += 1
        else:
            failed.append((row_n, f"{resp.status_code}: {resp.text[:80]}"))
        if n % BATCH == 0:
            flush()
            print(f"  -- {n}/{len(todo)} | pushed {pushed} | "
                  f"blocklisted {blocklisted} | failed {len(failed)}")
        time.sleep(0.15)
    flush()

    print("\n=== Summary ===")
    print(f"  Pushed:       {pushed}")
    print(f"  Blocklisted:  {blocklisted}")
    print(f"  Dupes:        {skipped_dupe}")
    print(f"  Domain-held:  {len(skipped_domain)}")
    print(f"  Failed:       {len(failed)}")
    for f in failed[:10]:
        print("   ", f)


if __name__ == "__main__":
    main()
