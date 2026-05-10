"""
Find personal DM emails via AnyMail Finder. No generic fallbacks — strict
personal-only policy.

Logic:
  1. Has DM name (col_dm_name) → AMF /find-email/person with name + domain
  2. No DM name, has website → AMF /find-email/decision-maker with domain (category=ceo)
  3. AMF returns nothing → skip row (no info@ fallback written)

Run:
  python3 -W ignore find_emails.py \
    --sheet_url "URL" --tab "TAB" \
    --col_name 0 --col_website 12 \
    --col_dm_name 13 --col_email 16 --col_first 17 --col_last 18
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


def col_letter(idx):
    if idx < 26: return chr(65 + idx)
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


def get_sheet_id(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def domain_from_url(url):
    if not url: return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.replace("www.", "").lower()


def split_name(full_name):
    parts = full_name.strip().split()
    if not parts: return "", ""
    if len(parts) == 1: return parts[0], ""
    return parts[0], " ".join(parts[1:])


def find_person_email(api_key, full_name, domain, company_name):
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
        return {"email": None, "status": status}
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def find_dm_email(api_key, domain, company_name):
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
            "status": status,
            "name": data.get("person_full_name", ""),
            "title": data.get("person_job_title", ""),
            "linkedin": data.get("person_linkedin_url", ""),
        }
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def flush_updates(service, updates, sheet_id, tab,
                  col_dm_name, col_dm_title, col_dm_linkedin,
                  col_email, col_first, col_last):
    if not updates:
        return
    data = []
    for u in updates:
        cells = u["cells"]
        for col_idx, val in cells.items():
            data.append({"range": f"'{tab}'!{col_letter(col_idx)}{u['row']}", "values": [[val]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} emails", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Find personal emails via AMF (no generic fallback)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_name", type=int, default=0)
    parser.add_argument("--col_website", type=int, required=True)
    parser.add_argument("--col_dm_name", type=int, required=True)
    parser.add_argument("--col_dm_title", type=int, default=-1, help="Set -1 to skip writing DM title")
    parser.add_argument("--col_dm_linkedin", type=int, default=-1)
    parser.add_argument("--col_email", type=int, required=True)
    parser.add_argument("--col_first", type=int, required=True)
    parser.add_argument("--col_last", type=int, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    api_key = os.getenv("ANYMAILFINDER_API_KEY")
    if not api_key:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); sys.exit(1)

    sheet_id = get_sheet_id(args.sheet_url)
    service = get_service()

    print("=== Find Personal Emails (AMF, no fallback) ===\n", flush=True)
    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{args.tab}'!A:AZ"
    ).execute()
    rows = result.get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[args.col_name] if len(row) > args.col_name else ""
        website = row[args.col_website] if len(row) > args.col_website else ""
        dm_name = row[args.col_dm_name] if len(row) > args.col_dm_name else ""
        email = row[args.col_email] if len(row) > args.col_email else ""
        if not name.strip() or not website.strip(): continue
        if email.strip(): continue
        targets.append({
            "row": i+2, "name": name.strip(),
            "domain": domain_from_url(website.strip()),
            "dm_name": dm_name.strip(),
        })

    if args.limit:
        targets = targets[:args.limit]

    with_name = sum(1 for t in targets if t["dm_name"])
    without = len(targets) - with_name
    print(f"  {len(targets)} to enrich: {with_name} with DM name (/person), {without} without (/decision-maker)\n", flush=True)
    if not targets:
        print("Nothing to do."); return

    print(f"[2/3] Calling AMF ({MAX_WORKERS} parallel workers)...", flush=True)
    updates = []
    found = nf = 0

    def process(t):
        domain = t["domain"]
        if t["dm_name"]:
            r = find_person_email(api_key, t["dm_name"], domain, t["name"])
            if r.get("email"):
                first, last = split_name(t["dm_name"])
                return t, r["email"], first, last, {}, "person"
        else:
            r = find_dm_email(api_key, domain, t["name"])
            if r.get("email"):
                first, last = split_name(r.get("name", "")) if r.get("name") else ("", "")
                extra = {}
                if r.get("name") and not t["dm_name"]:
                    extra[args.col_dm_name] = r["name"]
                if args.col_dm_title >= 0 and r.get("title"):
                    extra[args.col_dm_title] = r["title"]
                if args.col_dm_linkedin >= 0 and r.get("linkedin"):
                    extra[args.col_dm_linkedin] = r["linkedin"]
                return t, r["email"], first, last, extra, "dm"
        return t, None, "", "", {}, "not_found"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process, t): t for t in targets}
        for fut in as_completed(futures):
            t, email, first, last, extra, mode = fut.result()
            if email:
                found += 1
                cells = {args.col_email: email, args.col_first: first, args.col_last: last}
                cells.update(extra)
                updates.append({"row": t["row"], "cells": cells})
                print(f"  +  {t['name'][:38]:38s} [{mode}] -> {email}", flush=True)
            else:
                nf += 1

            if len(updates) >= BATCH:
                flush_updates(service, updates, sheet_id, args.tab,
                              args.col_dm_name, args.col_dm_title, args.col_dm_linkedin,
                              args.col_email, args.col_first, args.col_last)
                updates = []

    if updates:
        flush_updates(service, updates, sheet_id, args.tab,
                      args.col_dm_name, args.col_dm_title, args.col_dm_linkedin,
                      args.col_email, args.col_first, args.col_last)

    print(f"\n[3/3] Summary: Found {found}/{len(targets)}, No email {nf}", flush=True)


if __name__ == "__main__":
    main()
