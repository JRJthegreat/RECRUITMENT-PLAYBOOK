"""
Patch already-pushed lender leads in Instantly to update first_name +
personalization with the new email-name-parsing logic.

Reads the lender sheet (col C=email, AB=dm_name, AE=campaign_body).
For each lead in the SBA Lenders campaign, looks up by email and PATCHes:
  - first_name (from dm_name → email parse → fallback "team")
  - personalization (the re-rendered body)

Run:
  python3 -W ignore patch_lender_leads.py --campaign_id <ID>
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
SHEET_ID = "1-FxOuYFeI7xu76tcwkFAIcDVJuLSZg-5lBWcrqh7pIo"
TAB = "dataset_usda-lenders_2026-04-15_15-07-12-086"
ELIGIBLE_SIZES = {"1-10", "11-50", "51-200"}

ROLE_LOCAL_PARTS = {
    "info", "contact", "sales", "admin", "office", "hello", "hi",
    "support", "hr", "billing", "accounting", "invoices", "invoice",
    "mail", "general", "inquiries", "inquiry", "service", "help",
    "reception", "team", "enquiries", "noreply", "no-reply",
    "webmaster", "postmaster", "feedback", "careers", "jobs",
    "press", "media", "partners", "partnerships", "main", "mailbox",
    "cservice", "customerservice", "custserv", "membership",
    "memberservices", "loans", "loan", "lending", "credit", "banking",
    "tellers", "mortgage", "mortgagelenders", "citizens",
}


def first_name_from(full_name, fallback=""):
    if not full_name or not full_name.strip():
        return fallback
    return full_name.strip().split()[0]


def first_name_from_email(email):
    """Extract a usable name from email local part. Returns None for role-based addresses."""
    if not email or "@" not in email:
        return None
    local = email.split("@")[0].lower().strip()
    local = re.sub(r"[\d_\-]+$", "", local).rstrip("._-")
    if local in ROLE_LOCAL_PARTS:
        return None
    if "." in local or "_" in local:
        first = re.split(r"[._]+", local)[0]
        if first.isalpha() and len(first) >= 2 and first not in ROLE_LOCAL_PARTS:
            return first.capitalize()
        return None
    if not local.isalpha():
        return None
    if 2 <= len(local) <= 4:
        if local[0] in "aeiou" or local[1] in "aeiou":
            return local.capitalize()
        return None
    # 5+ chars: if first 2 are both consonants, strip leading initial → use lastname
    if len(local) >= 5:
        if local[0] not in "aeiou" and local[1] not in "aeiou":
            return local[1:].capitalize()
        return local.capitalize()
    return None


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


def list_leads(api_key, campaign_id):
    """Page through all leads in campaign. Returns list of {id, email}."""
    leads = []
    starting_after = None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    while True:
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        resp = requests.post(f"{INSTANTLY_BASE}/leads/list", headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        for it in items:
            leads.append({"id": it["id"], "email": it.get("email", "").lower()})
        next_starting = data.get("next_starting_after")
        if not next_starting or len(items) < 100:
            break
        starting_after = next_starting
    return leads


def patch_lead(api_key, lead_id, payload):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.patch(f"{INSTANTLY_BASE}/leads/{lead_id}", headers=headers, json=payload, timeout=30)
    return resp.status_code in (200, 204), resp.status_code, resp.text[:200]


def main():
    parser = argparse.ArgumentParser(description="Patch lender Instantly leads with corrected first_name + body")
    parser.add_argument("--campaign_id", default="1ae5c846-b338-48ca-a387-bfa91d5bc87f")
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not set"); sys.exit(1)

    print(f"=== Patch Lender Leads in Instantly ===\n", flush=True)
    service = get_service()

    print(f"[1/4] Reading lender sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AF"
    ).execute()
    rows = result.get("values", [])[1:]

    # Build email -> {first_name, body} map for qualifying lenders
    sheet_data = {}
    for row in rows:
        name = row[0] if len(row) > 0 else ""
        email_field = row[2] if len(row) > 2 else ""
        size = row[5] if len(row) > 5 else ""
        dm_name = row[27] if len(row) > 27 else ""
        body = row[30] if len(row) > 30 else ""
        added = row[31] if len(row) > 31 else ""

        if not (name.strip() and email_field.strip() and body.strip()):
            continue
        if size.strip() not in ELIGIBLE_SIZES:
            continue
        if not added.strip():
            continue  # not pushed yet

        first_email = email_field.strip().split(";")[0].split(",")[0].strip().lower()
        fname = first_name_from(dm_name, fallback="")
        if not fname:
            fname = first_name_from_email(first_email) or ""

        sheet_data[first_email] = {
            "first_name": fname or "team",
            "personalization": body.strip(),
        }

    print(f"  {len(sheet_data)} lenders pushed and ready to patch", flush=True)

    print(f"\n[2/4] Listing leads in campaign {args.campaign_id}...", flush=True)
    instantly_leads = list_leads(api_key, args.campaign_id)
    print(f"  {len(instantly_leads)} leads in campaign", flush=True)

    # Build email -> lead_id
    email_to_id = {l["email"]: l["id"] for l in instantly_leads}

    # Match
    to_patch = []
    missing = 0
    for email, payload in sheet_data.items():
        if email in email_to_id:
            to_patch.append((email, email_to_id[email], payload))
        else:
            missing += 1

    print(f"\n[3/4] Matched {len(to_patch)} leads to patch (missing {missing})", flush=True)

    if args.dry_run:
        print(f"\n=== DRY RUN — sample 5 patches ===")
        for email, lead_id, payload in to_patch[:5]:
            print(f"\n  {email} (id={lead_id})")
            print(f"    first_name -> {payload['first_name']}")
            print(f"    body preview -> {payload['personalization'][:80]}...")
        return

    print(f"\n[4/4] Patching {len(to_patch)} leads...", flush=True)
    ok = fail = 0
    for i, (email, lead_id, payload) in enumerate(to_patch):
        success, status, text = patch_lead(api_key, lead_id, payload)
        if success:
            ok += 1
        else:
            fail += 1
            print(f"  ! {email} failed ({status}): {text}", flush=True)
        if (i + 1) % 20 == 0:
            print(f"  Progress: {i+1}/{len(to_patch)} ({ok} ok, {fail} failed)", flush=True)
        time.sleep(0.05)

    print(f"\n=== Summary: {ok} patched, {fail} failed ===", flush=True)


if __name__ == "__main__":
    main()
