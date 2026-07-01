"""
One-off: find a decision-maker + email for the company-only rows on the
"Company Pages (No Contact)" tab of the SIA Attendees sheet.

These rows have NO named person (col C holds junk like "12,472 followers").
We call the AMF DECISION-MAKER endpoint (category=ceo — the buyer at a
staffing agency) by company domain, and write:
    A first_name | B last_name | C job_title | L email

ONLY when a valid email is found (email_status == "valid"). No partial data:
name/title are never written without a valid email. Risky/not_found are skipped.

Batch-of-10 writes; reruns skip rows that already have an email.

Run:
  python3 -W ignore enrich_sia_company_dms.py --sheet_url "URL" \
      --tab "Company Pages (No Contact)" [--preview] [--limit N]
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
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

HOSTING_DOMAINS = {
    "squarespace.com", "wix.com", "wixsite.com", "weebly.com", "wordpress.com",
    "webflow.io", "webflow.com", "godaddy.com", "shopify.com", "myshopify.com",
    "netlify.app", "vercel.app", "github.io", "carrd.co", "linktr.ee",
}

COL_FIRST   = 0   # A
COL_LAST    = 1   # B
COL_TITLE   = 2   # C  (currently junk "N followers" — overwritten only on success)
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


def root_domain(domain):
    parts = domain.lower().strip().split(".")
    two = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in two:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def split_name(full):
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def find_dm(domain, company):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"decision_maker_category": ["ceo"]}
    if domain:
        body["domain"] = domain
    if company:
        body["company_name"] = company
    if not domain and not company:
        return {"email": None, "status": "missing_data"}
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        status = d.get("email_status", "unknown")
        if not (email and status == "valid"):
            return {"email": None, "status": status or "not_found"}
        return {
            "email": email,
            "status": "valid",
            "name": d.get("person_full_name", "") or "",
            "title": d.get("person_job_title", "") or "",
        }
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def col_letter(idx):
    return chr(65 + idx)


def flush(service, updates, sheet_id, tab):
    if not updates:
        return
    data = []
    for u in updates:
        first, last = split_name(u["name"])
        data.append({"range": f"'{tab}'!{col_letter(COL_FIRST)}{u['row']}", "values": [[first]]})
        data.append({"range": f"'{tab}'!{col_letter(COL_LAST)}{u['row']}", "values": [[last]]})
        if u["title"]:
            data.append({"range": f"'{tab}'!{col_letter(COL_TITLE)}{u['row']}", "values": [[u["title"]]]})
        data.append({"range": f"'{tab}'!{col_letter(COL_EMAIL)}{u['row']}", "values": [[u["email"]]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> wrote {len(updates)} DMs this flush", flush=True)
    time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Company Pages (No Contact)")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="Reprocess rows that already have an email (replace with fresh CEO lookup)")
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
        company = cell(COL_COMPANY)
        email = cell(COL_EMAIL)
        domain = extract_domain(cell(COL_WEBSITE))
        if root_domain(domain) in HOSTING_DOMAINS:
            domain = ""
        if not company:
            continue
        if email and not args.overwrite:   # skip already-enriched unless overwriting
            continue
        if not (domain or company):
            continue
        targets.append({"row": i + 2, "company": company, "domain": domain})

    if args.limit:
        targets = targets[:args.limit]

    print(f"=== SIA Tab-2 DM finder — {len(targets)} company rows (decision-maker/ceo) ===\n", flush=True)
    for t in targets:
        print(f"  row {t['row']:>3} | {t['company'][:38]:<38} | {t['domain'] or '(no domain — use company name)'}")

    if args.preview:
        print(f"\n[PREVIEW] No API calls. {len(targets)} AMF decision-maker lookups would run.")
        return

    print("\n--- Running AMF decision-maker lookups ---\n", flush=True)
    updates = []
    found = 0
    rejected = []

    def run(t):
        return t, find_dm(t["domain"], t["company"])

    with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
        futures = [ex.submit(run, t) for t in targets]
        for fut in as_completed(futures):
            t, res = fut.result()
            if res["email"]:
                found += 1
                print(f"  +  {t['company'][:32]:<32} → {res['name']} ({res['title'][:24]}) | {res['email']}", flush=True)
                updates.append({"row": t["row"], "name": res["name"], "title": res["title"], "email": res["email"]})
            else:
                rejected.append((t, res["status"]))
                print(f"  -  {t['company'][:32]:<32} → [{res['status']}] (not written)", flush=True)
            if len(updates) >= WRITE_BATCH:
                flush(service, updates, sheet_id, tab); updates = []

    if updates:
        flush(service, updates, sheet_id, tab)

    print(f"\n=== Done: {found} DMs with valid email written, {len(rejected)} rejected ===")
    if rejected:
        from collections import Counter
        print("Rejected breakdown:")
        for status, n in Counter(s for _, s in rejected).items():
            print(f"  {status}: {n}")


if __name__ == "__main__":
    main()
