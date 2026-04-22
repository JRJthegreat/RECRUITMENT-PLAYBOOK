"""
Phase 5: Push tech leads from Google Sheets → Instantly campaign

Reads leads with email + Body from the sheet, creates a fresh Indeed-specific
Instantly campaign (or uses --campaign_id), adds leads, marks rows as added.

No CAMPAIGN_MAP — campaign is always created per run via --campaign_name.
"""

import os
import sys
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service():
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
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def list_sending_accounts(api_key):
    resp = requests.get(
        f"{INSTANTLY_BASE}/accounts",
        headers=instantly_headers(api_key),
        params={"limit": 100, "status": 1},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [a["email"] for a in items if a.get("email")]


def create_campaign(api_key, name, sending_accounts):
    """Create a fresh tech Indeed campaign (4-step sequence, same shape as hr-leads-indeed)."""
    payload = {
        "name": name,
        "email_list": sending_accounts,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "New schedule",
                    "timing": {"from": "09:00", "to": "18:00"},
                    "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
                    "timezone": "America/Detroit",
                }
            ]
        },
        "sequences": [
            {
                "steps": [
                    {
                        "type": "email",
                        "delay": 2,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "\u200b{{firstName}}, quick one",
                                "body": "<div>{{personalization}}\u00a0<br /><br />Sent from my iPhone<br /><br /><br /></div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}} ,\u00a0<br /><br />just bumping this - still working on the {{Role}} search?<br />\u00a0<br />Happy to send over the details.<br /><br />Best,<br />Jude<br /><br />Sent from my iPhone</div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}}, real quick one \u2014 still exploring candidates for the {{Role}} role or did you find someone?</div><div><br /></div><div>Happy to send over the profile if you're still searching.</div><div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 1,
                        "delay_unit": "days",
                        "pre_delay_unit": "days",
                        "variants": [
                            {
                                "subject": "",
                                "body": "<div>{{firstName}},\u00a0</div><div>I'll assume timing's not right. If the {{Role}} role opens back up later, feel free to reach out - happy to send over the profile.</div><div><br /></div><div>Best,</div><div>Jude</div><div><br /></div><div>Sent from my iPhone</div>",
                            }
                        ],
                    },
                ]
            }
        ],
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
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    data = resp.json()
    return data.get("id")


def push_lead(api_key, campaign_id, lead):
    payload = dict(lead)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(
            f"{INSTANTLY_BASE}/leads",
            headers=instantly_headers(api_key),
            json=payload,
            timeout=30,
        )
        return resp.status_code == 200, resp.status_code, resp.text[:200]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def add_leads(api_key, campaign_id, leads, service, sheet_id, added_col, leads_to_push):
    """Push leads one at a time, mark sheet every 10, retry failures up to 3x."""
    added = 0
    BATCH_SIZE = 10
    failed_indices = []

    for i, lead in enumerate(leads):
        ok, status_code, text = push_lead(api_key, campaign_id, lead)
        if ok:
            added += 1
        else:
            print(f"  Lead {i+1} failed ({status_code}): {text}")
            failed_indices.append((i, lead))

        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            failed_set = {idx for idx, _ in failed_indices}
            updates = []
            for j in range(batch_start, i + 1):
                if j not in failed_set:
                    updates.append({
                        "range": f"{added_col}{leads_to_push[j]['row_num']}",
                        "values": [["TRUE"]],
                    })
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
                            print(f"  Sheet write failed for batch: {e}")
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {len(failed_indices)} failed) → written to sheet")
            if i + 1 < len(leads):
                time.sleep(1.5)

    if failed_indices:
        print(f"\n  Retrying {len(failed_indices)} failed leads...")
        for attempt in range(1, 4):
            still_failing = []
            time.sleep(5 * attempt)
            for orig_idx, lead in failed_indices:
                ok, status_code, text = push_lead(api_key, campaign_id, lead)
                if ok:
                    added += 1
                    try:
                        service.spreadsheets().values().batchUpdate(
                            spreadsheetId=sheet_id,
                            body={"valueInputOption": "RAW", "data": [{
                                "range": f"{added_col}{leads_to_push[orig_idx]['row_num']}",
                                "values": [["TRUE"]],
                            }]},
                        ).execute()
                    except Exception:
                        pass
                else:
                    still_failing.append((orig_idx, lead))
            print(f"  Retry {attempt}: {len(failed_indices) - len(still_failing)} recovered, {len(still_failing)} still failing")
            failed_indices = still_failing
            if not failed_indices:
                break

    return added, len(failed_indices)


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def main():
    parser = argparse.ArgumentParser(description="Push tech leads from Google Sheets to a fresh Instantly campaign")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--campaign_name", required=True, help="Name for the new Instantly campaign")
    parser.add_argument("--campaign_id", help="Use existing campaign ID instead of creating new")
    parser.add_argument("--limit", type=int, default=0, help="Max leads (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview without creating/pushing")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("Error: INSTANTLY_API_KEY not set in .env")
        sys.exit(1)

    if not os.path.exists(TOKEN_PATH):
        print(f"Error: Google OAuth token not found at {TOKEN_PATH}")
        sys.exit(1)

    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service()

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"  Using tab: '{tab_name}'")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AC"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    COLUMN_ALIASES = {
        "email":              ["Email", "email"],
        "First name":         ["First Name", "First name"],
        "Last name":          ["Last Name", "Last name"],
        "Body":               ["Email Body", "Body"],
        "company name":       ["Company Name", "company name"],
        "job_title":          ["Job Title", "job_title"],
        "url":                ["Apply URL", "url"],
        "linkedin_url":       ["LinkedIn URL", "linkedin_url"],
        "result_title":       ["DM Title", "result_title"],
        "Added to instantly": ["Added to Instantly", "Added to instantly"],
        "cleaned_role":       ["cleaned_role"],
        "template_variant":   ["template_variant"],
    }

    def col_idx(canonical):
        for alias in COLUMN_ALIASES.get(canonical, [canonical]):
            try:
                return headers.index(alias)
            except ValueError:
                continue
        return None

    idx_email = col_idx("email")
    idx_firstname = col_idx("First name")
    idx_lastname = col_idx("Last name")
    idx_body = col_idx("Body")
    idx_company = col_idx("company name")
    idx_title = col_idx("job_title")
    idx_url = col_idx("url")
    idx_linkedin = col_idx("linkedin_url")
    idx_result_title = col_idx("result_title")
    idx_added = col_idx("Added to instantly")
    idx_cleaned_role = col_idx("cleaned_role")

    missing = []
    for name, idx in [("email", idx_email), ("First name", idx_firstname),
                       ("Body", idx_body), ("Added to instantly", idx_added)]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        sys.exit(1)

    leads_to_push = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cellv(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        email_addr = cellv(idx_email)
        body = cellv(idx_body)
        added = cellv(idx_added)

        if not email_addr or not body:
            continue
        if email_addr == "not_found":
            continue
        if added:
            continue

        first_name = cellv(idx_firstname)
        last_name = cellv(idx_lastname)
        company = cellv(idx_company)
        job_title = cellv(idx_title)
        cleaned_role = cellv(idx_cleaned_role) if idx_cleaned_role is not None else ""
        role = cleaned_role if cleaned_role else job_title
        job_link = cellv(idx_url)
        linkedin_url = cellv(idx_linkedin)
        dm_title = cellv(idx_result_title)

        leads_to_push.append({
            "row_num": i,
            "lead": {
                "email": email_addr,
                "first_name": first_name,
                "last_name": last_name,
                "company_name": "",
                "website": "",
                "personalization": body,
                "custom_variables": {
                    "Role": role,
                    "Job Link": job_link,
                    "Company name": company,
                    "LinkedIn_Url": linkedin_url,
                    "Decision Maker Title": dm_title,
                },
            },
        })

        if args.limit > 0 and len(leads_to_push) >= args.limit:
            break

    if not leads_to_push:
        print("No leads ready to push (need email + Body, not already added)")
        sys.exit(0)

    print(f"\nFound {len(leads_to_push)} leads ready to push")

    if args.dry_run:
        print(f"\n{'='*50}")
        print(f"DRY RUN — would create campaign '{args.campaign_name}'")
        print(f"Would add {len(leads_to_push)} leads:\n")
        for lp in leads_to_push[:5]:
            l = lp["lead"]
            print(f"  {l['first_name']} {l['last_name']} <{l['email']}>")
            print(f"    Role: {l['custom_variables']['Role']}")
            print(f"    Company: {l['custom_variables']['Company name']}")
            print(f"    DM Title: {l['custom_variables']['Decision Maker Title']}")
            print(f"    Personalization: {l['personalization'][:100]}...")
            print()
        if len(leads_to_push) > 5:
            print(f"  ... and {len(leads_to_push) - 5} more")
        print(f"{'='*50}")
        sys.exit(0)

    if args.campaign_id:
        campaign_id = args.campaign_id
        print(f"\nUsing existing campaign: {campaign_id}")
        accounts = []
    else:
        print("\nFetching sending accounts...")
        accounts = list_sending_accounts(api_key)
        if not accounts:
            print("Error: No active sending accounts found in Instantly")
            sys.exit(1)
        print(f"  Found {len(accounts)} active accounts")

        print(f"\nCreating campaign '{args.campaign_name}'...")
        campaign_id = create_campaign(api_key, args.campaign_name, accounts)
        print(f"  Campaign created: {campaign_id}")

    print(f"\nAdding {len(leads_to_push)} leads to campaign (batches of 10)...")
    added_col = f"'{tab_name}'!" + col_letter(idx_added)
    instantly_leads = [lp["lead"] for lp in leads_to_push]
    added_count, failed_count = add_leads(
        api_key, campaign_id, instantly_leads,
        service=service, sheet_id=sheet_id,
        added_col=added_col, leads_to_push=leads_to_push,
    )

    print(f"\n{'='*50}")
    print(f"Campaign Push Complete")
    print(f"  Campaign: {args.campaign_name}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"  Leads added: {added_count}")
    if failed_count:
        print(f"  Leads failed: {failed_count}")
    if accounts:
        print(f"  Sending accounts: {len(accounts)}")
    print(f"  Sheet updated: {len(leads_to_push)} rows marked")
    print(f"\n  Campaign created as DRAFT — activate in Instantly when ready.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
