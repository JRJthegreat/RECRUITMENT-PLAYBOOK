"""
Find decision makers via AnyMail Finder /decision-maker (niche-agnostic).

Every row already has a domain, so AMF returns the DM name + valid email in one
call. Valid emails only: DM name/title/linkedin are written ONLY when a valid
email comes back and its domain matches the company domain (no partial data,
no cross-company emails).

Reads:  --col_website (J), --col_domain (K, fallback)
Writes: --col_dm_name (AA), --col_dm_title (AB), --col_dm_email (AC),
        --col_dm_linkedin (AD), --col_status (AE)

Batch-of-10 writes. Resume-safe: skips rows where --col_status is already set.

Run:
  # sanity check first (1-3 credits):
  python3 -W ignore find_dm_amf.py --sheet_url "URL" --tab "TAB" --preview 3
  # then the test batch:
  python3 -W ignore find_dm_amf.py --sheet_url "URL" --tab "TAB" --limit 300
"""

import os
import re
import json
import time
import socket
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

socket.setdefaulttimeout(180)  # ride out transient network slowness on sheet reads/writes
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AMF_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_DM_URL  = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

WRITE_BATCH = 10
AMF_WORKERS = 8

# Hosting/builder platforms — never a real company domain.
HOSTING_DOMAINS = {
    "squarespace.com", "wix.com", "wixsite.com", "weebly.com", "wordpress.com",
    "webflow.io", "webflow.com", "godaddy.com", "shopify.com", "myshopify.com",
    "netlify.app", "vercel.app", "github.io", "carrd.co", "strikingly.com",
    "lovable.app", "framer.app", "framer.site", "bubble.io", "glide.page",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
    "mailchimp.com", "hubspot.com", "typeform.com",
}


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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
    two_part_tlds = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in two_part_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def email_matches_domain(email, company_domain):
    if not email or not company_domain:
        return False
    email_domain = email.split("@")[-1].lower().strip()
    return root_domain(email_domain) == root_domain(company_domain)


def get_domain(row, c_web, c_dom):
    website = row[c_web] if len(row) > c_web else ""
    domain = extract_domain(website)
    if not domain:
        raw = row[c_dom] if len(row) > c_dom else ""
        domain = extract_domain(raw) or raw.strip().lower()
    if not domain or root_domain(domain) in HOSTING_DOMAINS or domain in HOSTING_DOMAINS:
        return ""
    return domain


def amf_decision_maker(domain, category):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"decision_maker_category": [category], "domain": domain}
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        if d.get("email_status", "") != "valid":
            email = None
        # Reject cross-company emails.
        if email and not email_matches_domain(email, domain):
            email = None
        return {
            "dm_name":     d.get("person_full_name", "") or "",
            "dm_title":    d.get("person_job_title", "") or "",
            "dm_email":    email or "",
            "dm_linkedin": d.get("person_linkedin_url", "") or "",
        }
    except Exception as e:
        return {"dm_name": "", "dm_title": "", "dm_email": "", "dm_linkedin": "", "error": str(e)}


def ensure_headers(service, sheet_id, tab_name, cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=5)
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed = max(idx for idx, _ in cols) + 1
    if current_cols < needed:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": needed - current_cols,
            }}]},
        ).execute()
    for idx, header in cols:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()


def flush(service, updates, sheet_id, tab_name, cols):
    if not updates:
        return
    c_name, c_title, c_email, c_link, c_stat = cols
    data = []
    for u in updates:
        email = u.get("dm_email", "")
        # Only write name/title/linkedin when a valid email was found — never partial data.
        if email:
            if u.get("dm_name"):
                data.append({"range": f"'{tab_name}'!{col_letter(c_name)}{u['row']}", "values": [[u["dm_name"]]]})
            if u.get("dm_title"):
                data.append({"range": f"'{tab_name}'!{col_letter(c_title)}{u['row']}", "values": [[u["dm_title"]]]})
            if u.get("dm_linkedin"):
                data.append({"range": f"'{tab_name}'!{col_letter(c_link)}{u['row']}", "values": [[u["dm_linkedin"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(c_email)}{u['row']}", "values": [[email]]})
        data.append({"range": f"'{tab_name}'!{col_letter(c_stat)}{u['row']}", "values": [["found" if email else "not found"]]})
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
            ).execute()
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
            else:
                raise
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_website", default="J")
    parser.add_argument("--col_domain", default="K")
    parser.add_argument("--col_dm_name", default="AA")
    parser.add_argument("--col_dm_title", default="AB")
    parser.add_argument("--col_dm_email", default="AC")
    parser.add_argument("--col_dm_linkedin", default="AD")
    parser.add_argument("--col_status", default="AE")
    parser.add_argument("--category", default="ceo", help="AMF decision_maker_category (recruiters: ceo)")
    parser.add_argument("--limit", type=int, default=0, help="Cap rows processed (test batches)")
    parser.add_argument("--target_valid", type=int, default=0, help="Keep looking up until the sheet has this many total valid DM emails")
    parser.add_argument("--preview", type=int, default=0, help="Look up N and print without writing")
    args = parser.parse_args()

    if not AMF_API_KEY:
        raise SystemExit("ANYMAILFINDER_API_KEY not set in .env")

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    c_web  = col_to_idx(args.col_website)
    c_dom  = col_to_idx(args.col_domain)
    c_name = col_to_idx(args.col_dm_name)
    c_ttl  = col_to_idx(args.col_dm_title)
    c_eml  = col_to_idx(args.col_dm_email)
    c_lnk  = col_to_idx(args.col_dm_linkedin)
    c_stat = col_to_idx(args.col_status)
    out_cols = (c_name, c_ttl, c_eml, c_lnk, c_stat)

    service = get_service()
    if not args.preview:
        ensure_headers(service, sheet_id, tab_name, [
            (c_name, "dm_name"), (c_ttl, "dm_title"), (c_eml, "dm_email"),
            (c_lnk, "dm_linkedin_url"), (c_stat, "email_status"),
        ])

    last_col = col_letter(max(c_web, c_dom, *out_cols))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute(num_retries=5).get("values", [])[1:]

    pending, skipped_no_domain = [], 0
    for i, row in enumerate(rows):
        status = row[c_stat].strip() if len(row) > c_stat else ""
        if status:
            continue
        domain = get_domain(row, c_web, c_dom)
        if not domain:
            skipped_no_domain += 1
            continue
        pending.append({"row": i + 2, "domain": domain})

    print("=== Find Decision Makers (AMF /decision-maker) ===\n")

    if args.preview:
        for p in pending[:args.preview]:
            r = amf_decision_maker(p["domain"], args.category)
            print(f"{p['domain']:35}  {r.get('dm_name','') or '-':22}  {r.get('dm_title','') or '-':24}  {r.get('dm_email','') or 'NOT VALID'}")
        return

    # Target mode: keep going until the sheet holds --target_valid total valid emails.
    need = None
    existing_valid = 0
    if args.target_valid:
        existing_valid = sum(1 for row in rows if (row[c_stat].strip().lower() if len(row) > c_stat else "") == "found")
        need = max(0, args.target_valid - existing_valid)
        print(f"Already valid on sheet: {existing_valid}  |  target: {args.target_valid}  |  need {need} more")
        if need == 0:
            print("Target already met. Nothing to do.")
            return
    elif args.limit:
        pending = pending[:args.limit]

    print(f"Category: {args.category}  |  candidates available: {len(pending)}  |  skipped (no usable domain): {skipped_no_domain}\n")

    CHUNK = 24
    found = done = 0
    updates = []
    for start in range(0, len(pending), CHUNK):
        chunk = pending[start:start + CHUNK]
        with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
            futs = {ex.submit(amf_decision_maker, p["domain"], args.category): p for p in chunk}
            for fut in as_completed(futs):
                p = futs[fut]
                r = fut.result()
                r["row"] = p["row"]
                if r.get("dm_email"):
                    found += 1
                updates.append(r)
                done += 1
                if len(updates) >= WRITE_BATCH:
                    flush(service, updates, sheet_id, tab_name, out_cols)
                    updates = []
        print(f"  ...{done} looked up ({found} valid this run)", flush=True)
        if need is not None and found >= need:
            break
    if updates:
        flush(service, updates, sheet_id, tab_name, out_cols)

    if args.target_valid:
        print(f"\nDone - {found} new valid, total now ~{existing_valid + found}/{args.target_valid}.")
    else:
        print(f"\nDone - {found}/{done} rows got a valid DM email.")


if __name__ == "__main__":
    main()
