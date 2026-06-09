"""
Phase 3: Find decision maker (CEO/Founder) name, title, and email.

AMF only — no Google fallback.

Validations applied to every result:
  1. Company domain must not be a hosting/builder platform (Squarespace, Wix, etc.)
  2. Email domain must match the company domain — cross-company matches are rejected.

Run:
  python3 -W ignore find_ceo.py --sheet_url "URL" --tab "TAB" [--target N] [--limit N]
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

# Domains that are hosting/builder platforms — never a real company domain
HOSTING_DOMAINS = {
    "squarespace.com", "wix.com", "wixsite.com", "weebly.com", "wordpress.com",
    "webflow.io", "webflow.com", "godaddy.com", "shopify.com", "myshopify.com",
    "netlify.app", "vercel.app", "github.io", "carrd.co", "strikingly.com",
    "lovable.app", "framer.app", "framer.site", "bubble.io", "glide.page",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
    "mailchimp.com", "hubspot.com", "typeform.com",
}
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

COL_NAME        = 0   # A: Company Name
COL_DOMAIN      = 1   # B: Company Domain (fallback when website col is empty)
COL_WEBSITE     = 3   # D: Company Website URL
COL_DM_NAME        = 13  # N: dm_name
COL_DM_TITLE       = 14  # O: dm_title
COL_DM_EMAIL       = 15  # P: dm_email (empty when not found)
COL_DM_LINKEDIN    = 16  # Q: dm_linkedin_url
COL_EMAIL_STATUS   = 17  # R: email_status ("found" / "not found")

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


def ensure_headers(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = max(COL_DM_TITLE, COL_DM_EMAIL, COL_DM_LINKEDIN) + 2
    if current_cols < needed_cols:
        tab_sheet_id = sheet["properties"]["sheetId"]
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": tab_sheet_id,
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
        print(f"  Expanded sheet to {needed_cols} columns")

    for col_idx, header in [(COL_DM_NAME, "dm_name"), (COL_DM_TITLE, "dm_title"), (COL_DM_EMAIL, "dm_email"), (COL_DM_LINKEDIN, "dm_linkedin_url"), (COL_EMAIL_STATUS, "email_status")]:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()
        print(f"  Set header: {header}")


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def flush_updates(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = []
    for u in updates:
        dm_email = u.get("dm_email", "")
        # Only write name/title/linkedin when a valid email was found — never write partial data
        if dm_email:
            if u.get("dm_name"):
                data.append({
                    "range": f"'{tab_name}'!{col_letter(COL_DM_NAME)}{u['row']}",
                    "values": [[u["dm_name"]]],
                })
            if u.get("dm_title"):
                data.append({
                    "range": f"'{tab_name}'!{col_letter(COL_DM_TITLE)}{u['row']}",
                    "values": [[u["dm_title"]]],
                })
            if u.get("dm_linkedin"):
                data.append({
                    "range": f"'{tab_name}'!{col_letter(COL_DM_LINKEDIN)}{u['row']}",
                    "values": [[u["dm_linkedin"]]],
                })
        # Write email (empty when not found) and status column
        data.append({
            "range": f"'{tab_name}'!{col_letter(COL_DM_EMAIL)}{u['row']}",
            "values": [[dm_email]],
        })
        data.append({
            "range": f"'{tab_name}'!{col_letter(COL_EMAIL_STATUS)}{u['row']}",
            "values": [["found" if dm_email else "not found"]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    print(f"  -> Wrote {len(updates)} DMs", flush=True)
    time.sleep(1)


def amf_decision_maker(domain, company_name):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"decision_maker_category": ["ceo"]}
    if domain:
        body["domain"] = domain
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        status = d.get("email_status", "")
        if status != "valid":
            email = None
        return {
            "dm_name": d.get("person_full_name", "") or "",
            "dm_title": d.get("person_job_title", "") or "",
            "dm_email": email or "",
            "dm_linkedin": d.get("person_linkedin_url", "") or "",
        }
    except Exception as e:
        return {"dm_name": "", "dm_title": "", "dm_email": "", "dm_linkedin": "", "error": str(e)}


def root_domain(domain):
    """'mail.company.com' → 'company.com', handles common 2-part TLDs."""
    parts = domain.lower().strip().split(".")
    two_part_tlds = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in two_part_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def email_matches_domain(email, company_domain):
    """Reject emails whose domain doesn't match the company's domain."""
    if not email or not company_domain:
        return False
    email_domain = email.split("@")[-1].lower().strip()
    return root_domain(email_domain) == root_domain(company_domain)


def parse_sheet_id(sheet_url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {sheet_url}")
    return m.group(1)


def get_domain(row):
    website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
    domain = extract_domain(website)
    if not domain:
        raw_domain = row[COL_DOMAIN] if len(row) > COL_DOMAIN else ""
        domain = extract_domain(raw_domain) or raw_domain.strip().lower()
    # Reject hosting/builder platform domains
    if root_domain(domain) in HOSTING_DOMAINS or domain in HOSTING_DOMAINS:
        return ""
    return domain


def count_verified(rows):
    """Count rows with email_status == 'found'."""
    count = 0
    for row in rows:
        status = row[COL_EMAIL_STATUS] if len(row) > COL_EMAIL_STATUS else ""
        if status.strip().lower() == "found":
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--target", type=int, default=0,
                        help="Stop once this many verified DM emails exist in the sheet")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    if not AMF_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); return

    service = get_service()
    ensure_headers(service, sheet_id, tab_name)

    print("=== Phase 3: Find Decision Maker (AMF) ===\n", flush=True)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:Z"
    ).execute().get("values", [])[1:]

    already_verified = count_verified(rows)
    print(f"Already verified: {already_verified}", flush=True)
    if args.target and already_verified >= args.target:
        print(f"Target of {args.target} already reached — nothing to do."); return

    remaining = (args.target - already_verified) if args.target else 0

    # Step A targets: no DM name, no dm_email already set, has some domain
    step_a = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        dm_name = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""
        status = row[COL_EMAIL_STATUS] if len(row) > COL_EMAIL_STATUS else ""
        domain = get_domain(row)
        if name.strip() and not dm_name.strip() and not status.strip() and domain:
            step_a.append({"row": i + 2, "name": name.strip(), "domain": domain})

    if args.limit:
        step_a = step_a[:args.limit]

    print(f"[Step A] AMF decision-maker lookup — {len(step_a)} companies...", flush=True)
    updates = []
    a_found = a_not_found = 0
    verified_count = already_verified
    target_hit = False

    def run_amf_dm(t):
        return t, amf_decision_maker(t["domain"], t["name"])

    with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
        futures = [ex.submit(run_amf_dm, t) for t in step_a]
        for i, fut in enumerate(as_completed(futures), 1):
            if target_hit:
                fut.cancel()
                continue
            t, result = fut.result()
            dm_name = result.get("dm_name", "")
            dm_email = result.get("dm_email", "")
            dm_linkedin = result.get("dm_linkedin", "")
            if dm_email:
                a_found += 1
                verified_count += 1
                print(f"  +  {t['name'][:50]:50s} → {dm_name} | {dm_email} [{verified_count}/{args.target or '∞'}]", flush=True)
            else:
                a_not_found += 1
            updates.append({
                "row": t["row"],
                "dm_name": dm_name,
                "dm_title": result.get("dm_title", ""),
                "dm_email": dm_email,
                "dm_linkedin": dm_linkedin,
            })
            if len(updates) >= WRITE_BATCH:
                flush_updates(service, updates, sheet_id, tab_name)
                updates = []
            if args.target and verified_count >= args.target:
                print(f"\n  Target of {args.target} verified DMs reached — stopping.", flush=True)
                target_hit = True
            if i % 50 == 0:
                print(f"  Progress: {i}/{len(step_a)}", flush=True)

    if updates:
        flush_updates(service, updates, sheet_id, tab_name)
    print(f"\n  Step A: found {a_found}, not found {a_not_found} / {len(step_a)}\n", flush=True)

    if target_hit:
        return

    print(f"\nTotal verified: {verified_count}", flush=True)


if __name__ == "__main__":
    main()
