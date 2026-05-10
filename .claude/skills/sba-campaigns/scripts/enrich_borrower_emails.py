"""
Phase 3: Find emails for SBA rural borrowers via AnyMail Finder.

Two modes depending on what we have:
  - DM name found (col N): call /find-email/person with full name + domain → personal email
  - No DM name but have website (col M): call /find-email/decision-maker with domain only
    → AMF finds whoever it can + email, also writes back name/title if returned

Fallback for both: if AMF finds nothing and we have a domain, write
  info@domain as the contact email (generic but usable).

Reads:  col A=borrower_name, col M=website, col N=dm_name
Writes: col N=dm_name (if AMF returns one), col O=dm_title,
        col P=dm_linkedin, col Q=email, col R=first_name, col S=last_name

Resumable — skips rows already with email in col Q.

Run:
  python3 -W ignore enrich_borrower_emails.py [--limit N] [--no_fallback]
"""

import os
import sys
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
MAX_WORKERS = 10
BATCH = 10
SHEET_WRITE_DELAY = 1

SHEET_ID = "1WgIhmQmJ1XhYHIVb6DgPuvBG1ex1_k76fPvr9BBVfR0"
TAB = "dataset_sba-rural-loans_2026-04-16_05-40-32-227"

COL_NAME = 0; COL_WEBSITE = 12; COL_DM_NAME = 13; COL_DM_TITLE = 14
COL_DM_LINKEDIN = 15; COL_EMAIL = 16; COL_FIRST = 17; COL_LAST = 18


def split_name(full_name):
    parts = full_name.strip().split()
    if not parts: return "", ""
    if len(parts) == 1: return parts[0], ""
    return parts[0], " ".join(parts[1:])


def col_letter(idx):
    if idx < 26: return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def domain_from_url(url):
    if not url: return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.replace("www.", "").lower()


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


def find_person_email(api_key, full_name, domain, company_name):
    """AMF /find-email/person — use when we know the DM's name."""
    first, last = split_name(full_name)
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"full_name": full_name, "first_name": first, "last_name": last}
    if domain: body["domain"] = domain
    if company_name: body["company_name"] = company_name
    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        if email and status in ("valid", "risky"):
            return {"email": email, "status": status}
        return {"email": None, "status": status or "not_found"}
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def find_dm_email(api_key, domain, company_name):
    """AMF /find-email/decision-maker — use when we don't have a name."""
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"decision_maker_category": ["ceo"]}
    if domain: body["domain"] = domain
    if company_name: body["company_name"] = company_name
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("valid_email") or data.get("email")
        status = data.get("email_status", "unknown")
        return {
            "email": email if email and status in ("valid", "risky") else None,
            "status": status or "not_found",
            "name": data.get("person_full_name", ""),
            "title": data.get("person_job_title", ""),
            "linkedin": data.get("person_linkedin_url", ""),
        }
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def flush_updates(service, updates):
    if not updates:
        return
    data = []
    for u in updates:
        for col_idx, val in u["cells"].items():
            data.append({"range": f"'{TAB}'!{col_letter(col_idx)}{u['row']}", "values": [[val]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} records", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no_fallback", action="store_true",
                        help="Don't write generic info@ fallback email")
    args = parser.parse_args()

    api_key = os.getenv("ANYMAILFINDER_API_KEY")
    if not api_key:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); sys.exit(1)

    service = get_service()
    print("=== Enrich Borrower Emails (AMF person + decision-maker) ===\n", flush=True)

    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:S"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        dm_name = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""
        existing_email = row[COL_EMAIL] if len(row) > COL_EMAIL else ""

        if not name.strip() or not website.strip():
            continue
        if existing_email.strip():
            continue  # already enriched

        targets.append({
            "row": i+2,
            "name": name.strip(),
            "website": website.strip(),
            "domain": domain_from_url(website.strip()),
            "dm_name": dm_name.strip(),
        })

    if args.limit:
        targets = targets[:args.limit]

    print(f"  {len(targets)} borrowers to enrich", flush=True)
    print(f"    {sum(1 for t in targets if t['dm_name'])} have DM name → /find-email/person", flush=True)
    print(f"    {sum(1 for t in targets if not t['dm_name'])} no DM name → /find-email/decision-maker\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Calling AMF ({MAX_WORKERS} parallel workers)...", flush=True)
    updates = []
    found = fallback_used = nf = 0

    def process(t):
        domain = t["domain"]
        company = t["name"]
        if t["dm_name"]:
            # We know the person — use person endpoint
            r = find_person_email(api_key, t["dm_name"], domain, company)
            if r.get("email"):
                first, last = split_name(t["dm_name"])
                return t, r["email"], first, last, {}, "person"
        else:
            # No name — use decision-maker endpoint
            r = find_dm_email(api_key, domain, company)
            if r.get("email"):
                dm = r.get("name", "")
                first, last = split_name(dm) if dm else ("", "")
                extra = {}
                if dm and not t["dm_name"]:
                    extra[COL_DM_NAME] = dm
                if r.get("title"):
                    extra[COL_DM_TITLE] = r["title"]
                if r.get("linkedin"):
                    extra[COL_DM_LINKEDIN] = r["linkedin"]
                return t, r["email"], first, last, extra, "dm"
        # Fallback: generic info@ email
        return t, None, "", "", {}, "not_found"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, t): t for t in targets}
        for fut in as_completed(futures):
            t, email, first, last, extra, mode = fut.result()
            if email:
                found += 1
                cells = {COL_EMAIL: email, COL_FIRST: first, COL_LAST: last}
                cells.update(extra)
                updates.append({"row": t["row"], "cells": cells})
                print(f"  +  {t['name'][:38]:38s} [{mode}] -> {email}", flush=True)
            elif not args.no_fallback and t["domain"]:
                # Generic fallback
                fallback_email = f"info@{t['domain']}"
                fallback_used += 1
                updates.append({"row": t["row"], "cells": {COL_EMAIL: fallback_email, COL_FIRST: "", COL_LAST: ""}})
                print(f"  ~  {t['name'][:38]:38s} [fallback] -> {fallback_email}", flush=True)
            else:
                nf += 1
                print(f"  x  {t['name'][:38]:38s} -> (no email)", flush=True)

            if len(updates) >= BATCH:
                flush_updates(service, updates)
                updates = []

    if updates:
        flush_updates(service, updates)

    print(f"\n[3/3] Summary", flush=True)
    print(f"  AMF found:       {found}", flush=True)
    print(f"  Fallback info@:  {fallback_used}", flush=True)
    print(f"  No email:        {nf}", flush=True)
    print(f"  Total:           {found + fallback_used}/{len(targets)}", flush=True)


if __name__ == "__main__":
    main()
