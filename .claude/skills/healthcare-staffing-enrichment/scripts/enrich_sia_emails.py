"""
One-off: enrich missing decision-maker emails on the "List A - SIA Attendees" sheet.

Schema (this sheet only):
  A first_name  B last_name  C job_title  D company_name  E company_size
  F company_location  G company_website  H company_bio  I linkedin_url
  J post_snippet  K post_date  L email

We already know the person (name + company website), so we use the AMF
PERSON endpoint. Only email_status == "valid" is written — risky / not_found
are rejected (never written).

Batch-of-10 writes for crash recovery; reruns skip rows that already have an email.

Run:
  python3 -W ignore enrich_sia_emails.py --sheet_url "URL" --tab "Sheet1" [--preview] [--limit N]
"""

import os
import re
import json
import time
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AMF_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

COL_FIRST   = 0   # A
COL_LAST    = 1   # B
COL_TITLE   = 2   # C
COL_COMPANY = 3   # D
COL_WEBSITE = 6   # G
COL_EMAIL   = 12  # M (post_url inserted at J shifted email L->M)

WRITE_BATCH = 10
AMF_WORKERS = 8


def get_service():
    td = json.load(open(TOKEN_PATH))
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        json.dump(td, open(TOKEN_PATH, "w"))
    return build("sheets", "v4", credentials=creds)


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def find_email_person(first, last, domain, company):
    full_name = f"{first} {last}".strip()
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {}
    if full_name:
        body["full_name"] = full_name
    if first:
        body["first_name"] = first
    if last:
        body["last_name"] = last
    if domain:
        body["domain"] = domain
    if company:
        body["company_name"] = company

    if not full_name or not (domain or company):
        return {"email": None, "status": "missing_data"}
    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        if email and status == "valid":
            return {"email": email, "status": "valid"}
        return {"email": None, "status": status or "not_found"}
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def col_letter(idx):
    return chr(65 + idx)


def flush(service, updates, sheet_id, tab):
    if not updates:
        return
    data = [{
        "range": f"'{tab}'!{col_letter(COL_EMAIL)}{u['row']}",
        "values": [[u["email"]]],
    } for u in updates if u["email"]]
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    print(f"  -> wrote {len(data)} valid emails this flush", flush=True)
    time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Sheet1")
    ap.add_argument("--preview", action="store_true", help="List targets, make NO API calls")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not AMF_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); return

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:M"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        def cell(c):
            return row[c].strip() if len(row) > c else ""
        first, last = cell(COL_FIRST), cell(COL_LAST)
        company = cell(COL_COMPANY)
        email = cell(COL_EMAIL)
        domain = extract_domain(cell(COL_WEBSITE))
        if email:
            continue                      # already has email — skip (idempotent)
        if not (first or last):
            continue                      # no named person — skip
        if not (domain or company):
            continue                      # no way to anchor the lookup
        targets.append({
            "row": i + 2, "first": first, "last": last,
            "company": company, "domain": domain,
        })

    if args.limit:
        targets = targets[:args.limit]

    print(f"=== SIA email enrichment — {len(targets)} rows missing email (person endpoint) ===\n", flush=True)
    for t in targets:
        print(f"  row {t['row']:>3} | {t['first']} {t['last']:<18} | {t['company'][:32]:<32} | {t['domain'] or '(no domain — use company name)'}")

    if args.preview:
        print(f"\n[PREVIEW] No API calls made. {len(targets)} AMF person lookups would run.")
        return

    print("\n--- Running AMF person lookups ---\n", flush=True)
    updates = []
    found = 0
    rejected = []

    def run(t):
        return t, find_email_person(t["first"], t["last"], t["domain"], t["company"])

    with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
        futures = [ex.submit(run, t) for t in targets]
        for fut in as_completed(futures):
            t, res = fut.result()
            if res["email"]:
                found += 1
                print(f"  +  {t['first']} {t['last']:<18} {t['company'][:28]:<28} → {res['email']}", flush=True)
                updates.append({"row": t["row"], "email": res["email"]})
            else:
                rejected.append((t, res["status"]))
                print(f"  -  {t['first']} {t['last']:<18} {t['company'][:28]:<28} → [{res['status']}] (not written)", flush=True)
            if len(updates) >= WRITE_BATCH:
                flush(service, updates, sheet_id, tab); updates = []

    if updates:
        flush(service, updates, sheet_id, tab)

    print(f"\n=== Done: {found} valid emails written, {len(rejected)} rejected (risky/not_found) ===")
    if rejected:
        print("Rejected breakdown:")
        from collections import Counter
        for status, n in Counter(s for _, s in rejected).items():
            print(f"  {status}: {n}")


if __name__ == "__main__":
    main()
