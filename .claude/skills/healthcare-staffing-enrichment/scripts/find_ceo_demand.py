"""
Find CEO/Owner email for healthcare recruitment firms via AnyMail Finder
decision-maker endpoint, passing BOTH company domain and company name.

Schema (source sheet, A-based):
  A:Company Name  B:Headcount  C:Employee Size  D:Industry  E:Product/Services
  F:Description   G:SEO Description  H:Website  I:LinkedIn  J:X  K:Phone
  L:Founding Year  M:Annual Revenue  N:AI Ark Account ID
Appended output columns:
  O:dm_name  P:dm_title  Q:dm_email  R:dm_linkedin_url  S:email_status

Validations:
  1. Hosting/builder-platform domains rejected (Squarespace, Wix, etc.)
  2. Only email_status == "valid" is written (risky/unknown rejected).
  3. Email domain must match the company domain (cross-company rejected).
  4. Name/title/linkedin written only when a valid email was found (no partial data).

Run:
  python3 -W ignore find_ceo_demand.py --sheet_url "URL" --tab "1-50 EMP" --target 500
"""

import os
import re
import json
import time
import threading
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
    "netlify.app", "vercel.app", "github.io", "carrd.co", "strikingly.com",
    "lovable.app", "framer.app", "framer.site", "bubble.io", "glide.page",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
    "mailchimp.com", "hubspot.com", "typeform.com",
}

COL_NAME    = 0    # A
COL_WEBSITE = 7    # H
COL_DM_NAME     = 14  # O
COL_DM_TITLE    = 15  # P
COL_DM_EMAIL    = 16  # Q
COL_DM_LINKEDIN = 17  # R
COL_EMAIL_STATUS = 18 # S

WRITE_BATCH = 10
AMF_WORKERS = 8


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def parse_sheet_id(sheet_url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {sheet_url}")
    return m.group(1)


def ensure_headers(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = COL_EMAIL_STATUS + 2
    if current_cols < needed_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
    for col_idx, header in [
        (COL_DM_NAME, "dm_name"), (COL_DM_TITLE, "dm_title"),
        (COL_DM_EMAIL, "dm_email"), (COL_DM_LINKEDIN, "dm_linkedin_url"),
        (COL_EMAIL_STATUS, "email_status"),
    ]:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(col_idx)}1",
            valueInputOption="RAW", body={"values": [[header]]},
        ).execute()


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def root_domain(domain):
    parts = domain.lower().strip().split(".")
    two_part_tlds = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in two_part_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def email_matches_domain(email, company_domain):
    if not email or not company_domain:
        return False
    email_domain = email.split("@")[-1].lower().strip()
    return root_domain(email_domain) == root_domain(company_domain)


def get_domain(row):
    domain = extract_domain(row[COL_WEBSITE] if len(row) > COL_WEBSITE else "")
    if root_domain(domain) in HOSTING_DOMAINS or domain in HOSTING_DOMAINS:
        return ""
    return domain


def amf_decision_maker(domain, company_name):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"decision_maker_category": ["ceo"]}
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        if d.get("email_status", "") != "valid":
            email = None
        # cross-company guard
        if email and domain and not email_matches_domain(email, domain):
            email = None
        return {
            "dm_name": d.get("person_full_name", "") or "",
            "dm_title": d.get("person_job_title", "") or "",
            "dm_email": email or "",
            "dm_linkedin": d.get("person_linkedin_url", "") or "",
        }
    except Exception as e:
        return {"dm_name": "", "dm_title": "", "dm_email": "", "dm_linkedin": "", "error": str(e)}


def flush_updates(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = []
    for u in updates:
        dm_email = u.get("dm_email", "")
        if dm_email:
            if u.get("dm_name"):
                data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_NAME)}{u['row']}", "values": [[u["dm_name"]]]})
            if u.get("dm_title"):
                data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_TITLE)}{u['row']}", "values": [[u["dm_title"]]]})
            if u.get("dm_linkedin"):
                data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_LINKEDIN)}{u['row']}", "values": [[u["dm_linkedin"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_EMAIL)}{u['row']}", "values": [[dm_email]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_EMAIL_STATUS)}{u['row']}", "values": [["found" if dm_email else "not found"]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(1)


def count_verified(rows):
    c = 0
    for row in rows:
        status = row[COL_EMAIL_STATUS] if len(row) > COL_EMAIL_STATUS else ""
        if status.strip().lower() == "found":
            c += 1
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not AMF_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); return

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    ensure_headers(service, sheet_id, tab)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:S"
    ).execute().get("values", [])[1:]

    already = count_verified(rows)
    print(f"=== Find CEO/Owner (AMF decision-maker) — tab '{tab}' ===", flush=True)
    print(f"Already found: {already} / target {args.target}", flush=True)
    if already >= args.target:
        print("Target already reached — nothing to do."); return

    targets = []
    for i, row in enumerate(rows):
        name = (row[COL_NAME] if len(row) > COL_NAME else "").strip()
        status = (row[COL_EMAIL_STATUS] if len(row) > COL_EMAIL_STATUS else "").strip()
        domain = get_domain(row)
        if name and not status and domain:
            targets.append({"row": i + 2, "name": name, "domain": domain})
    if args.limit:
        targets = targets[:args.limit]

    print(f"Candidates to query: {len(targets)}", flush=True)
    updates, found, not_found = [], 0, 0
    verified = already
    stop_event = threading.Event()

    def run(t):
        # Short-circuit queued tasks once the target is reached — avoids
        # burning AMF credits on candidates we no longer need.
        if stop_event.is_set():
            return t, {"skip": True}
        return t, amf_decision_maker(t["domain"], t["name"])

    with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
        futures = [ex.submit(run, t) for t in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            t, result = fut.result()
            if result.get("skip"):
                continue
            dm_email = result.get("dm_email", "")
            if dm_email:
                found += 1; verified += 1
                print(f"  +  {t['name'][:45]:45s} -> {result.get('dm_name','')} | {dm_email} [{verified}/{args.target}]", flush=True)
            else:
                not_found += 1
            updates.append({
                "row": t["row"], "dm_name": result.get("dm_name", ""),
                "dm_title": result.get("dm_title", ""), "dm_email": dm_email,
                "dm_linkedin": result.get("dm_linkedin", ""),
            })
            if len(updates) >= WRITE_BATCH:
                flush_updates(service, updates, sheet_id, tab); updates = []
            if verified >= args.target:
                print(f"\nTarget of {args.target} reached — stopping.", flush=True)
                stop_event.set()
            if i % 100 == 0:
                print(f"  Progress: {i}/{len(targets)} (found {found}, not found {not_found})", flush=True)

    if updates:
        flush_updates(service, updates, sheet_id, tab)
    print(f"\nDone. Found this run: {found} | total verified: {verified}", flush=True)


if __name__ == "__main__":
    main()
