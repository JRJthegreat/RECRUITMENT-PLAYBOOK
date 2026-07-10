"""
Push recruitment leads to a new Instantly campaign (niche-agnostic).

Creates the campaign as DRAFT (Jude activates manually in the UI), then pushes
each valid lead one at a time. The generated body (col AJ) rides in as the
{{personalization}} variable; extras go in custom_variables.

NO subject line (empty on the first email, per Jude's spec). No em dashes.
NEVER adds sending accounts — Jude configures those in the UI.

Only pushes rows where status == found, dm_email present, and body present.
Resume-safe: skips rows where --col_added is already TRUE. Writes TRUE per batch.

Run:
  # preview first (no API calls):
  python3 -W ignore push_campaign.py --sheet_url "URL" --tab "TAB" \
    --campaign_name "AU Recruitment - Test" --dry_run
  # then for real:
  python3 -W ignore push_campaign.py --sheet_url "URL" --tab "TAB" \
    --campaign_name "AU Recruitment - Test" [--limit N]
"""

import os
import re
import json
import time
import socket
import argparse
import requests
from dotenv import load_dotenv

socket.setdefaulttimeout(180)  # ride out transient network slowness on sheet reads/writes
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

# Sequence copy — niche-neutral, no em dashes, no subject line.
STEP1_BODY = "<div>{{personalization}} <br /><br />Sent from my iPhone</div>"
STEP2_BODY = ("<div>{{firstName}},<br /><br />just bumping this, still open to taking on new reqs?"
              "<br /><br />Happy to make the intro.<br /><br />Best,<br />Jude<br /><br />Sent from my iPhone</div>")
STEP3_BODY = ("<div>{{firstName}}, real quick, still looking for fresh reqs or all good on pipeline?</div>"
              "<div><br /></div><div>Happy to connect you if you're still searching.</div>"
              "<div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>")
STEP4_BODY = ("<div>{{firstName}},</div><div>I'll assume timing's not right. If you're ever looking for "
              "fresh reqs down the line, feel free to reach out.</div>"
              "<div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>")


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


def cell(row, idx):
    return row[idx].strip() if idx is not None and idx < len(row) and row[idx] else ""


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


def make_step(body, delay):
    return {
        "type": "email", "delay": delay, "delay_unit": "days", "pre_delay_unit": "days",
        "variants": [{"subject": "", "body": body}],   # empty subject = no subject line
    }


def create_campaign(name, timezone):
    payload = {
        "name": name,
        "campaign_schedule": {"schedules": [{
            "name": "New schedule",
            "timing": {"from": "09:00", "to": "18:00"},
            "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
            "timezone": timezone,
        }]},
        "sequences": [{"steps": [
            make_step(STEP1_BODY, 2),
            make_step(STEP2_BODY, 2),
            make_step(STEP3_BODY, 1),
            make_step(STEP4_BODY, 1),
        ]}],
        "daily_limit": 100,
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


def push_lead(campaign_id, lead_payload):
    payload = dict(lead_payload)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(), json=payload, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def ensure_added_col(service, sheet_id, tab_name, c_added):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    if current_cols < c_added + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": (c_added + 1) - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(c_added)}1",
        valueInputOption="RAW", body={"values": [["added_to_instantly"]]},
    ).execute()


def mark_added(service, sheet_id, tab_name, c_added, row_nums):
    if not row_nums:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(c_added)}{r}", "values": [["TRUE"]]} for r in row_nums]
    for attempt in range(3):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                print(f"  Sheet write failed: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--campaign_name", required=True)
    parser.add_argument("--col_company", default="A")
    parser.add_argument("--col_website", default="J")
    parser.add_argument("--col_company_linkedin", default="L")
    parser.add_argument("--col_dm_title", default="AB")
    parser.add_argument("--col_dm_email", default="AC")
    parser.add_argument("--col_dm_linkedin", default="AD")
    parser.add_argument("--col_status", default="AE")
    parser.add_argument("--status_value", default="found")
    parser.add_argument("--col_first", default="AF")
    parser.add_argument("--col_last", default="AG")
    parser.add_argument("--col_body", default="AJ")
    parser.add_argument("--col_added", default="AK")
    # Instantly's allowed-timezone list is limited (rejects e.g. Australia/Sydney).
    # America/Detroit is known-good; the real sending window is set in the Instantly UI.
    parser.add_argument("--timezone", default="America/Detroit")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    if not INSTANTLY_API_KEY:
        raise SystemExit("INSTANTLY_API_KEY not set in .env")

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    c_comp   = col_to_idx(args.col_company)
    c_web    = col_to_idx(args.col_website)
    c_clink  = col_to_idx(args.col_company_linkedin)
    c_ttl    = col_to_idx(args.col_dm_title)
    c_email  = col_to_idx(args.col_dm_email)
    c_dlink  = col_to_idx(args.col_dm_linkedin)
    c_stat   = col_to_idx(args.col_status)
    c_first  = col_to_idx(args.col_first)
    c_last   = col_to_idx(args.col_last)
    c_body   = col_to_idx(args.col_body)
    c_added  = col_to_idx(args.col_added)
    status_value = args.status_value.strip().lower()

    print("=== Push Recruitment Leads -> Instantly ===\n")
    service = get_service()
    if not args.dry_run:
        ensure_added_col(service, sheet_id, tab_name, c_added)

    last_col = col_letter(max(c_comp, c_web, c_clink, c_ttl, c_email, c_dlink, c_stat, c_first, c_last, c_body, c_added))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]
    print(f"Total rows: {len(rows)}")

    leads = []
    for i, row in enumerate(rows):
        if args.limit and len(leads) >= args.limit:
            break
        if cell(row, c_stat).lower() != status_value:
            continue
        email = cell(row, c_email)
        body  = cell(row, c_body)
        if not email or not body:
            continue
        if cell(row, c_added).upper() == "TRUE":
            continue
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":           email,
                "first_name":      cell(row, c_first),
                "last_name":       cell(row, c_last),
                "company_name":    cell(row, c_comp),
                "website":         cell(row, c_web),
                "personalization": body,
                "custom_variables": {
                    "Company name":     cell(row, c_comp),
                    "DM Title":         cell(row, c_ttl),
                    "dm_linkedin":      cell(row, c_dlink),
                    "company_linkedin": cell(row, c_clink),
                },
            },
        })

    print(f"Leads to push: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['first_name']} {p['last_name']} <{p['email']}>  ({p['company_name']})")
            print("    " + p["personalization"][:220].replace(chr(10), " / ") + " ...")
            print()
        print(f"[DRY RUN] Would create DRAFT campaign '{args.campaign_name}' ({args.timezone}) and push {len(leads)} leads. No API calls made.")
        return

    print(f"Creating DRAFT campaign '{args.campaign_name}'...")
    campaign_id = create_campaign(args.campaign_name, args.timezone)
    print(f"  Campaign created: {campaign_id}\n")

    added, failed = 0, []
    pending_rows = []
    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
            pending_rows.append(lead["row_num"])
        else:
            print(f"  Lead {i+1} failed ({status}): {text}")
            failed.append(lead)
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            mark_added(service, sheet_id, tab_name, c_added, pending_rows)
            pending_rows = []
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {len(failed)} failed)", flush=True)
            if i + 1 < len(leads):
                time.sleep(1.5)

    if failed:
        print(f"\n  Retrying {len(failed)} failed leads...")
        for attempt in range(1, 4):
            still = []
            time.sleep(5 * attempt)
            recovered = []
            for lead in failed:
                ok, _, _ = push_lead(campaign_id, lead["payload"])
                if ok:
                    added += 1
                    recovered.append(lead["row_num"])
                else:
                    still.append(lead)
            mark_added(service, sheet_id, tab_name, c_added, recovered)
            print(f"  Retry {attempt}: {len(failed) - len(still)} recovered, {len(still)} still failing")
            failed = still
            if not failed:
                break

    print(f"\n=== Done ===")
    print(f"  Pushed:      {added}")
    print(f"  Failed:      {len(failed)}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"\n  Campaign created as DRAFT — activate in Instantly when ready.")
    print(f"  Reminder: add sending accounts manually in the UI (never done by script).")


if __name__ == "__main__":
    main()
