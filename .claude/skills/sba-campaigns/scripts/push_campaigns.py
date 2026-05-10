"""
Push leads from Google Sheets to Instantly v2 campaigns.

Two modes — same script handles both:

  --campaign lenders   USDA lenders (164 small banks, role-based emails)
  --campaign borrowers SBA rural borrowers (138 with verified websites)

Pattern matches scrape-hr-leads/scripts/push_campaign.py:
  - Lists active sending accounts
  - Creates a new campaign (default Mon-Fri 9-18 ET, 2-day delay)
  - Pushes leads one at a time, marks "added_to_instantly" col every 10
  - Retries failed leads up to 3 times

Run:
  python3 -W ignore push_campaigns.py --campaign lenders \
      --campaign_name "USDA Lenders Cosign Mar 2026" [--dry_run]
  python3 -W ignore push_campaigns.py --campaign borrowers \
      --campaign_name "SBA Rural Borrowers Mar 2026" [--dry_run]
"""

import os
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

LENDER_SHEET_ID = "1-FxOuYFeI7xu76tcwkFAIcDVJuLSZg-5lBWcrqh7pIo"
LENDER_TAB = "dataset_usda-lenders_2026-04-15_15-07-12-086"
BORROWER_SHEET_ID = "1WgIhmQmJ1XhYHIVb6DgPuvBG1ex1_k76fPvr9BBVfR0"
BORROWER_TAB = "dataset_sba-rural-loans_2026-04-16_05-40-32-227"

ELIGIBLE_LENDER_SIZES = {"1-10", "11-50", "51-200"}


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


def instantly_headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def list_sending_accounts(api_key):
    resp = requests.get(
        f"{INSTANTLY_BASE}/accounts",
        headers=instantly_headers(api_key),
        params={"limit": 100, "status": 1},
        timeout=30,
    )
    resp.raise_for_status()
    return [a["email"] for a in resp.json().get("items", []) if a.get("email")]


def create_campaign(api_key, name, sending_accounts):
    """Create campaign with default schedule. Body uses {{personalization}} so
    Jude's per-lead campaign_body cell drives the message."""
    payload = {
        "name": name,
        "email_list": sending_accounts,
        "campaign_schedule": {
            "schedules": [{
                "name": "New schedule",
                "timing": {"from": "09:00", "to": "18:00"},
                "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
                "timezone": "America/Detroit",
            }]
        },
        "sequences": [{
            "steps": [{
                "type": "email", "delay": 2,
                "delay_unit": "days", "pre_delay_unit": "days",
                "variants": [{
                    "subject": "​{{firstName}}, quick one",
                    "body": "<div>{{personalization}} <br /><br />Sent from my iPhone<br /><br /><br /></div>",
                }],
            }, {
                "type": "email", "delay": 1,
                "delay_unit": "days", "pre_delay_unit": "days",
                "variants": [{
                    "subject": "",
                    "body": "<div>{{firstName}}, <br /><br />just bumping this back to the top - want me to share more details?<br /><br />Best,<br />Jude<br /><br />Sent from my iPhone</div>",
                }],
            }, {
                "type": "email", "delay": 1,
                "delay_unit": "days", "pre_delay_unit": "days",
                "variants": [{
                    "subject": "",
                    "body": "<div>{{firstName}},</div><div>I'll assume timing's not right. If anything changes, feel free to reach out.</div><div><br /></div><div>Best,</div><div>Jude</div></div>",
                }],
            }]
        }],
        "daily_limit": 2500,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": True,
        "first_email_text_only": True,
        "prioritize_new_leads": False,
        "stop_for_company": False,
    }
    resp = requests.post(
        f"{INSTANTLY_BASE}/campaigns",
        headers=instantly_headers(api_key),
        json=payload, timeout=30,
    )
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json().get("id")


def push_lead(api_key, campaign_id, lead):
    payload = dict(lead)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(
            f"{INSTANTLY_BASE}/leads",
            headers=instantly_headers(api_key),
            json=payload, timeout=30,
        )
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def add_leads(api_key, campaign_id, leads_to_push, service, sheet_id, tab, added_col_idx):
    """Add leads one at a time, mark added_to_instantly col every 10."""
    added = 0
    BATCH_SIZE = 10
    failed = []  # (idx, lead)
    added_col = f"'{tab}'!{col_letter(added_col_idx)}"

    for i, lp in enumerate(leads_to_push):
        ok, status, text = push_lead(api_key, campaign_id, lp["lead"])
        if ok:
            added += 1
        else:
            print(f"  Lead {i+1} failed ({status}): {text}")
            failed.append((i, lp))

        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads_to_push):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            failed_set = {idx for idx, _ in failed}
            updates = [
                {"range": f"{added_col}{leads_to_push[j]['row']}", "values": [["TRUE"]]}
                for j in range(batch_start, i + 1)
                if j not in failed_set
            ]
            if updates:
                for attempt in range(3):
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id,
                            body={"valueInputOption": "RAW", "data": updates},
                        ).execute()
                        break
                    except Exception as e:
                        if attempt < 2:
                            time.sleep(5)
                        else:
                            print(f"  Sheet write failed: {e}")
            print(f"  Progress: {i+1}/{len(leads_to_push)} ({added} added, {len(failed)} failed)")
            if i + 1 < len(leads_to_push):
                time.sleep(1.5)

    if failed:
        print(f"\n  Retrying {len(failed)} failed leads...")
        for attempt in range(1, 4):
            still = []
            time.sleep(5 * attempt)
            for orig_i, lp in failed:
                ok, status, text = push_lead(api_key, campaign_id, lp["lead"])
                if ok:
                    added += 1
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id,
                            body={"valueInputOption": "RAW", "data": [{
                                "range": f"{added_col}{lp['row']}",
                                "values": [["TRUE"]],
                            }]},
                        ).execute()
                    except Exception:
                        pass
                else:
                    still.append((orig_i, lp))
            print(f"  Retry {attempt}: {len(failed) - len(still)} recovered, {len(still)} still failing")
            failed = still
            if not failed:
                break

    return added, len(failed)


def build_lender_leads(rows):
    leads = []
    for i, row in enumerate(rows):
        name = row[0] if len(row) > 0 else ""
        email = row[2] if len(row) > 2 else ""
        size = row[5] if len(row) > 5 else ""
        city = row[10] if len(row) > 10 else ""
        state = row[12] if len(row) > 12 else ""
        dm_name = row[27] if len(row) > 27 else ""
        dm_title = row[28] if len(row) > 28 else ""
        body = row[30] if len(row) > 30 else ""
        added = row[31] if len(row) > 31 else ""

        if not name.strip() or not email.strip() or not body.strip():
            continue
        if size.strip() not in ELIGIBLE_LENDER_SIZES:
            continue
        if added.strip():
            continue

        # Use first email if column has multiple separated by ; or ,
        email_addr = email.strip().split(";")[0].split(",")[0].strip()

        leads.append({
            "row": i + 2,
            "lead": {
                "email": email_addr,
                "first_name": "team",  # role-based inbox
                "last_name": "",
                "company_name": name.strip(),
                "personalization": body.strip(),
                "custom_variables": {
                    "Company": name.strip(),
                    "City": city.strip(),
                    "State": state.strip(),
                    "DM_Name": dm_name.strip(),
                    "DM_Title": dm_title.strip(),
                },
            },
        })
    return leads


def build_borrower_leads(rows):
    leads = []
    for i, row in enumerate(rows):
        name = row[0] if len(row) > 0 else ""
        city = row[1] if len(row) > 1 else ""
        state = row[2] if len(row) > 2 else ""
        loan_amount = row[3] if len(row) > 3 else ""
        naics = row[6] if len(row) > 6 else ""
        email = row[16] if len(row) > 16 else ""
        first = row[17] if len(row) > 17 else ""
        last = row[18] if len(row) > 18 else ""
        body = row[19] if len(row) > 19 else ""
        added = row[20] if len(row) > 20 else ""

        if not name.strip() or not email.strip() or not body.strip():
            continue
        if added.strip():
            continue

        leads.append({
            "row": i + 2,
            "lead": {
                "email": email.strip(),
                "first_name": first.strip() or "there",
                "last_name": last.strip(),
                "company_name": name.strip(),
                "personalization": body.strip(),
                "custom_variables": {
                    "Company": name.strip(),
                    "City": city.strip(),
                    "State": state.strip(),
                    "Loan_Amount": str(loan_amount).strip(),
                    "Industry": naics.strip(),
                },
            },
        })
    return leads


def main():
    parser = argparse.ArgumentParser(description="Push SBA campaigns to Instantly")
    parser.add_argument("--campaign", choices=["lenders", "borrowers"], required=True)
    parser.add_argument("--campaign_name", required=True)
    parser.add_argument("--campaign_id", help="Reuse existing campaign instead of creating")
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not set in .env"); sys.exit(1)

    if args.campaign == "lenders":
        sheet_id = LENDER_SHEET_ID
        tab = LENDER_TAB
        added_col_idx = 31  # AF
    else:
        sheet_id = BORROWER_SHEET_ID
        tab = BORROWER_TAB
        added_col_idx = 20  # U

    service = get_service()

    print(f"=== Push {args.campaign.title()} to Instantly ===\n", flush=True)
    print(f"[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:AF"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"  {len(rows)} total rows", flush=True)

    if args.campaign == "lenders":
        leads = build_lender_leads(rows)
    else:
        leads = build_borrower_leads(rows)

    print(f"  {len(leads)} leads ready to push (have email + body, not pushed)\n", flush=True)
    if not leads:
        print("Nothing to push."); return

    if args.dry_run:
        print(f"=== DRY RUN — would create campaign '{args.campaign_name}' ===")
        print(f"Would add {len(leads)} leads. Sample of 5:\n")
        for lp in leads[:5]:
            l = lp["lead"]
            print(f"  {l['first_name']} {l['last_name']} <{l['email']}>")
            print(f"    Company: {l['custom_variables']['Company']}")
            print(f"    City/State: {l['custom_variables']['City']}, {l['custom_variables']['State']}")
            print(f"    Body preview: {l['personalization'][:120]}...")
            print()
        if len(leads) > 5:
            print(f"  ...and {len(leads) - 5} more")
        return

    if args.campaign_id:
        campaign_id = args.campaign_id
        accounts = []
        print(f"[2/3] Using existing campaign: {campaign_id}", flush=True)
    else:
        print(f"[2/3] Fetching sending accounts...", flush=True)
        accounts = list_sending_accounts(api_key)
        if not accounts:
            print("ERROR: No active sending accounts in Instantly"); sys.exit(1)
        print(f"  {len(accounts)} active accounts", flush=True)
        print(f"  Creating campaign '{args.campaign_name}'...", flush=True)
        campaign_id = create_campaign(api_key, args.campaign_name, accounts)
        print(f"  Campaign created: {campaign_id}", flush=True)

    print(f"\n[3/3] Adding {len(leads)} leads...", flush=True)
    added, failed = add_leads(api_key, campaign_id, leads, service, sheet_id, tab, added_col_idx)

    print(f"\n=== Summary ===")
    print(f"  Campaign: {args.campaign_name}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"  Added: {added} / {len(leads)}")
    if failed:
        print(f"  Failed: {failed}")
    if accounts:
        print(f"  Sending accounts: {len(accounts)}")
    print(f"\n  Campaign created as DRAFT — activate in Instantly UI when ready.")


if __name__ == "__main__":
    main()
