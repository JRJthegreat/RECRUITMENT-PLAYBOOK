"""
Create the healthcare recruitment-firm EXPANSION campaign in Instantly (DRAFT)
and push valid leads one at a time — the Aug 2026 clone of
push_demand_campaign.py (per repo convention copy scripts are cloned, never
parameterized; do NOT edit push_demand_campaign.py).

Mirrors the May 25th Supply campaign (01f0b596 — Jude's best on opportunities):
subject "Awesome work at {{companyName}}" (Jude 2026-08-05: no greeting
prefix), "Sent from my iPhone" footer on every step, delays 2/2/5. Greetings
are "Hi" not "hey" (Jude 2026-08-05). Follow-up copy reworked to the
expansion frame (newly opened clinics staffing up new locations) approved
2026-08-04.

Settings: Mon-Sat 07:00-18:00 America/Detroit, daily_limit 500, stop_on_reply,
no link/open tracking, text_only + first_email_text_only.
Lead personalization (col V) is plain text with newlines, not HTML.

Lead payload:
  email, first_name, last_name, company_name, website, personalization (email body),
  custom_variables: { job_title, dm_linkedin, company_linkedin }

Schema (1-50 EMP tab):
  A:Company Name  H:Website  I:LinkedIn(company)  P:dm_title  Q:dm_email
  R:dm_linkedin   S:email_status  T:first_name  U:last_name  V:email_body  W:added_to_instantly

Data guards:
  1. A found row is pushed only if its email domain matches its website
     domain (catches scrambled/mismatched rows). Skipped rows are logged.
  2. Duplicate emails are skipped — both within this push (multi-location
     companies sharing one CEO) and against rows already pushed in any
     earlier campaign (col W == TRUE anywhere in the tab).

Resume-safe: skips rows where col W == "TRUE" or "BLOCKLISTED".
Leads rejected by Instantly's workspace blocklist (permanent 400) are marked
col W = "BLOCKLISTED" immediately and not retried.

Run:
  python3 -W ignore push_demand_campaign.py --sheet_url "URL" --tab "TAB" \
    --campaign_name "NAME" [--campaign_id ID] [--limit N] [--dry_run]
"""

import os
import re
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"
BATCH_SIZE = 10

# company_name feeds {{companyName}} in the SUBJECT — it must be the
# casualized form ("premier", not "Premier Search, Inc."), exactly like the
# May 25th campaign. Reuse the same casualizer the body generator uses.
from generate_expansion_body import casual_company

COL_COMPANY      = 0   # A
COL_WEBSITE      = 7   # H
COL_CO_LINKEDIN  = 8   # I
COL_DM_TITLE     = 15  # P
COL_DM_EMAIL     = 16  # Q
COL_DM_LINKEDIN  = 17  # R
COL_EMAIL_STATUS = 18  # S
COL_FIRST        = 19  # T
COL_LAST         = 20  # U
COL_BODY         = 21  # V
COL_ADDED        = 22  # W

SUBJECT = "Awesome work at {{companyName}}"

IPHONE = "<div><br /></div><div>Sent from my iPhone</div>"

# Signature lives at SEQUENCE level, never in the per-lead body (Jude
# 2026-08-05). Above the iPhone line on every step.
SIGNATURE = ("<div><br /></div><div>Best regards,</div>"
             "<div>Rood Judeley (Jude)</div>")

STEP1_BODY = "<div>{{personalization}}</div>" + SIGNATURE + IPHONE

FU1 = ("<div>Hi {{firstName}},</div><div><br /></div>"
       "<div>Just bumping this up in case it got buried.</div><div><br /></div>"
       "<div>The clinics I mentioned are staffing up their new locations now, and they'd "
       "rather have recruiters lined up before the doors open than scramble after.</div><div><br /></div>"
       "<div>Let me know if you are open to some warm intros.</div>" + SIGNATURE + IPHONE)

FU2 = ("<div>Hi {{firstName}},</div><div><br /></div>"
       "<div>Last note from me.</div><div><br /></div>"
       "<div>No worries if connecting with these clinics is not a priority right now.</div>"
       "<div><br /></div>"
       "<div>When timing is right, feel free to reopen. I am one reply away.</div>" + SIGNATURE + IPHONE)


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def instantly_headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


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


def root_domain(domain):
    parts = domain.lower().strip().split(".")
    two = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in two:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def create_campaign(name):
    payload = {
        "name": name,
        "campaign_schedule": {"schedules": [{
            "name": "Default",
            "timing": {"from": "07:00", "to": "18:00"},
            "days": {"1": True, "2": True, "3": True, "4": True, "5": True, "6": True},
            "timezone": "America/Detroit",
        }]},
        "sequences": [{"steps": [
            {"type": "email", "delay": 2, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": SUBJECT, "body": STEP1_BODY}]},
            {"type": "email", "delay": 2, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "", "body": FU1}]},
            {"type": "email", "delay": 5, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "", "body": FU2}]},
        ]}],
        "daily_limit": 500,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": True,
        "first_email_text_only": True,
        "prioritize_new_leads": False,
        "stop_for_company": False,
    }
    resp = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=instantly_headers(), json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()["id"]


def push_lead(campaign_id, payload):
    p = dict(payload)
    p["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(), json=p, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def ensure_col_w(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    if current_cols < COL_ADDED + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": (COL_ADDED + 1) - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!{col_letter(COL_ADDED)}1",
        valueInputOption="RAW", body={"values": [["added_to_instantly"]]},
    ).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--campaign_name", required=True)
    ap.add_argument("--campaign_id", default="", help="Push into an existing campaign instead of creating one")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set"); return

    service = get_service()
    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    ensure_col_w(service, sheet_id, tab)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:W"
    ).execute().get("values", [])[1:]
    print(f"Total rows: {len(rows)}")

    # emails already pushed in any earlier campaign — never push twice
    seen_emails = {cell(r, COL_DM_EMAIL).lower() for r in rows
                   if cell(r, COL_ADDED).upper() == "TRUE" and cell(r, COL_DM_EMAIL)}

    leads, skipped, skipped_dupe = [], 0, 0
    for i, row in enumerate(rows):
        if args.limit and len(leads) >= args.limit:
            break
        if cell(row, COL_EMAIL_STATUS).lower() != "found":
            continue
        if cell(row, COL_ADDED).upper() in ("TRUE", "BLOCKLISTED", "PRIOR_CAMPAIGN"):
            continue
        email = cell(row, COL_DM_EMAIL)
        body  = cell(row, COL_BODY)
        if not email or not body:
            continue
        if email.lower() in seen_emails:
            skipped_dupe += 1
            print(f"  SKIP (duplicate email) row {i+2}: {cell(row, COL_COMPANY)[:30]} | {email}")
            continue
        # data guard: email domain must match website domain
        wd = root_domain(extract_domain(cell(row, COL_WEBSITE)))
        ed = root_domain(email.split("@")[-1]) if "@" in email else ""
        if not wd or wd != ed:
            skipped += 1
            print(f"  SKIP (domain mismatch) row {i+2}: {cell(row, COL_COMPANY)[:30]} | web={cell(row, COL_WEBSITE)} | {email}")
            continue
        seen_emails.add(email.lower())
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":           email,
                "first_name":      cell(row, COL_FIRST),
                "last_name":       cell(row, COL_LAST),
                "company_name":    casual_company(cell(row, COL_COMPANY)),
                "website":         cell(row, COL_WEBSITE),
                "personalization": body,
                "custom_variables": {
                    "job_title":        cell(row, COL_DM_TITLE),
                    "dm_linkedin":      cell(row, COL_DM_LINKEDIN),
                    "company_linkedin": cell(row, COL_CO_LINKEDIN),
                },
            },
        })

    print(f"Leads to push: {len(leads)} | skipped (mismatch): {skipped} | skipped (dupe email): {skipped_dupe}\n")

    if args.dry_run:
        for lead in leads[:3]:
            p = lead["payload"]; cv = p["custom_variables"]
            print(f"  Row {lead['row_num']}: {p['email']}")
            print(f"    name:             {p['first_name']} {p['last_name']}")
            print(f"    company_name:     {p['company_name']}")
            print(f"    website:          {p['website']}")
            print(f"    job_title:        {cv['job_title']}")
            print(f"    dm_linkedin:      {cv['dm_linkedin']}")
            print(f"    company_linkedin: {cv['company_linkedin']}")
            print(f"    personalization:  {p['personalization'][:120]}...")
            print()
        print("[DRY RUN] No campaign created, no leads pushed.")
        return

    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"Using existing campaign: {campaign_id}\n")
    else:
        print(f"Creating campaign '{args.campaign_name}' (DRAFT)...")
        campaign_id = create_campaign(args.campaign_name)
        print(f"  Campaign created: {campaign_id}\n")

    added, blocklisted_idx, failed = 0, set(), []
    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
        elif status == 400 and "blocklist" in text.lower():
            blocklisted_idx.add(i)
            print(f"  Lead {i+1} BLOCKLISTED (permanent): {lead['payload']['email']}")
            try:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [{
                        "range": f"'{tab}'!{col_letter(COL_ADDED)}{lead['row_num']}", "values": [["BLOCKLISTED"]]}]}).execute()
            except Exception as e:
                print(f"  Sheet write failed for BLOCKLISTED mark: {e}")
        else:
            print(f"  Lead {i+1} failed ({status}): {text}")
            failed.append((i, lead))
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            failed_idx = {idx for idx, _ in failed} | blocklisted_idx
            updates = [{"range": f"'{tab}'!{col_letter(COL_ADDED)}{ld['row_num']}", "values": [["TRUE"]]}
                       for j, ld in enumerate(leads[batch_start:i + 1], batch_start) if j not in failed_idx]
            if updates:
                for attempt in range(3):
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": updates}).execute()
                        break
                    except Exception as e:
                        if attempt < 2: time.sleep(5)
                        else: print(f"  Sheet write failed: {e}")
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {len(failed)} failed)")
            if i + 1 < len(leads):
                time.sleep(1.2)

    if failed:
        print(f"\n  Retrying {len(failed)} failed leads...")
        for attempt in range(1, 4):
            still = []
            time.sleep(5 * attempt)
            for orig_idx, lead in failed:
                ok, _, _ = push_lead(campaign_id, lead["payload"])
                if ok:
                    added += 1
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [{
                                "range": f"'{tab}'!{col_letter(COL_ADDED)}{lead['row_num']}", "values": [["TRUE"]]}]}).execute()
                    except Exception: pass
                else:
                    still.append((orig_idx, lead))
            print(f"  Retry {attempt}: {len(failed)-len(still)} recovered, {len(still)} still failing")
            failed = still
            if not failed: break

    print(f"\n=== Done ===")
    print(f"  Pushed:      {added}")
    print(f"  Blocklisted: {len(blocklisted_idx)}")
    print(f"  Failed:      {len(failed)}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"\n  Campaign is DRAFT — add sending accounts and activate in Instantly UI.")


if __name__ == "__main__":
    main()
