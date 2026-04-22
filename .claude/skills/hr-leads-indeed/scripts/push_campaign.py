"""
Phase 3b: Push leads from Google Sheets → Instantly campaign

Reads leads with email + Body from the sheet, creates an Instantly campaign,
adds leads matching the reference campaign structure, and marks rows.
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

# Load .env from the skill's parent .claude directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service(token_path):
    with open(token_path) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(token_path, "w") as f:
            json.dump(token_data, f)

    return build("sheets", "v4", credentials=creds)


def instantly_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def list_sending_accounts(api_key):
    """Get active sending accounts from Instantly."""
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
    """Create campaign matching reference structure (Reyna_Mar 5th)."""
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


def shorten_role(job_title):
    """Shorten job title to how a recruiter would say it in conversation."""
    title = (job_title or "").strip()
    # Common shortenings
    replacements = [
        ("Human Resources", "HR"),
        ("Human Resource", "HR"),
        ("Business Partner", "BP"),
        ("Director of ", "Dir. of "),
        ("Talent Acquisition", "TA"),
        ("Vice President", "VP"),
    ]
    for old, new in replacements:
        title = title.replace(old, new)

    # Strip parentheticals, numbering prefixes
    import re
    title = re.sub(r'\s*\(.*?\)', '', title)  # Remove (Remote), (HRBP), etc.
    title = re.sub(r'^\d+\s*[-–]\s*', '', title)  # Remove "194 - " prefix
    title = re.sub(r'\s*[-–]\s*GTM$', '', title)  # Remove "- GTM" suffix
    title = title.strip(" -–,")

    # HRBP shortcut
    if "HR" in title and "BP" in title:
        if "Global" in title or "Senior" in title or "Sr" in title:
            prefix = ""
            if "Global" in title:
                prefix = "Global "
            elif "Senior" in title or "Sr" in title:
                prefix = "Sr. "
            title = f"{prefix}HRBP"
        elif "HRBP" not in title:
            title = "HRBP"

    return title


def push_lead(api_key, campaign_id, lead):
    """Push a single lead to Instantly. Returns True on success."""
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


def add_leads(api_key, campaign_id, leads, service=None, sheet_id=None, added_col=None, leads_to_push=None):
    """Add leads to campaign one at a time via v2 API, writing to sheet every 10.
    Retries failed leads up to 3 times with 5s delay."""
    added = 0
    BATCH_SIZE = 10
    failed_indices = []  # track (original_index, lead) for retry

    for i, lead in enumerate(leads):
        ok, status_code, text = push_lead(api_key, campaign_id, lead)
        if ok:
            added += 1
        else:
            print(f"  Lead {i+1} failed ({status_code}): {text}")
            failed_indices.append((i, lead))

        # Every 10 leads (or at end), mark successfully added ones in sheet
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            if service and sheet_id and added_col and leads_to_push:
                # Only mark leads that were not in failed_indices
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
            # 1.5s delay between sheet batch writes
            if i + 1 < len(leads):
                time.sleep(1.5)

    # Retry failed leads up to 3 times
    if failed_indices:
        print(f"\n  Retrying {len(failed_indices)} failed leads...")
        for attempt in range(1, 4):
            still_failing = []
            time.sleep(5 * attempt)
            for orig_idx, lead in failed_indices:
                ok, status_code, text = push_lead(api_key, campaign_id, lead)
                if ok:
                    added += 1
                    # Mark in sheet
                    if service and sheet_id and added_col and leads_to_push:
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


def main():
    parser = argparse.ArgumentParser(description="Push leads from Google Sheets to Instantly campaign")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--campaign_name", required=True, help="Name for the Instantly campaign")
    parser.add_argument("--campaign_id", help="Use existing campaign ID instead of creating new")
    parser.add_argument("--variant", choices=["A", "B"], help="Only push leads with this template_variant")
    parser.add_argument("--dry_run", action="store_true", help="Preview without creating campaign")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("Error: INSTANTLY_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Detect tab
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    tab_name = "Leads" if "Data" not in tab_titles and "Leads" in tab_titles else "Data"
    print(f"  Using tab: '{tab_name}'")

    # Read all data
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab_name
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    COLUMN_ALIASES = {
        "email":              ["email", "Email"],
        "First name":         ["First name", "First Name"],
        "Last name":          ["Last name", "Last Name"],
        "Body":               ["Body", "Email Body"],
        "company name":       ["company name", "Company Name"],
        "job_title":          ["job_title", "Job Title"],
        "url":                ["url", "Apply URL"],
        "company_linkedin_url":["company_linkedin_url", "Company LinkedIn"],
        "linkedin_url":       ["linkedin_url", "LinkedIn URL"],
        "result_title":       ["result_title", "DM Title"],
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
    idx_company_linkedin = col_idx("company_linkedin_url")
    idx_linkedin = col_idx("linkedin_url")
    idx_result_title = col_idx("result_title")
    idx_added = col_idx("Added to instantly")
    idx_cleaned_role = col_idx("cleaned_role")
    idx_variant = col_idx("template_variant")

    missing = []
    for name, idx in [("email", idx_email), ("First name", idx_firstname),
                       ("Body", idx_body), ("Added to instantly", idx_added)]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        sys.exit(1)

    # Collect leads ready to push
    leads_to_push = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        email_addr = cell(idx_email)
        body = cell(idx_body)
        added = cell(idx_added)

        # Skip if no email, no body, or already added
        if not email_addr or not body:
            continue
        if added:
            continue

        # Filter by variant if --variant flag set
        if args.variant:
            row_variant = cell(idx_variant) if idx_variant is not None else ""
            if row_variant.upper() != args.variant:
                continue

        first_name = cell(idx_firstname)
        last_name = cell(idx_lastname)
        company = cell(idx_company)
        job_title = cell(idx_title)
        cleaned_role = cell(idx_cleaned_role) if idx_cleaned_role is not None else ""
        role = cleaned_role if cleaned_role else shorten_role(job_title)
        job_link = cell(idx_url)
        company_linkedin = cell(idx_company_linkedin)
        linkedin_url = cell(idx_linkedin)
        dm_title = cell(idx_result_title)

        # personalization = full email body as-is from the Body column
        personalization = body

        leads_to_push.append({
            "row_num": i,
            "lead": {
                "email": email_addr,
                "first_name": first_name,
                "last_name": last_name,
                "company_name": "",
                "website": "",
                "personalization": personalization,
                "custom_variables": {
                    "Role": role,
                    "Job Link": job_link,
                    "Company name": company,
                    "LinkedIn_Url": linkedin_url,
                    "Company_Linkedin": company_linkedin,
                    "Decision Maker Title": dm_title,
                },
            },
        })

    if not leads_to_push:
        print("No leads ready to push (need email + Body, not already added)")
        sys.exit(0)

    print(f"\nFound {len(leads_to_push)} leads ready to push")

    # Dry run
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
        # Get sending accounts
        print("\nFetching sending accounts...")
        accounts = list_sending_accounts(api_key)
        if not accounts:
            print("Error: No active sending accounts found in Instantly")
            sys.exit(1)
        print(f"  Found {len(accounts)} active accounts")

        # Create campaign
        print(f"\nCreating campaign '{args.campaign_name}'...")
        campaign_id = create_campaign(api_key, args.campaign_name, accounts)
        print(f"  Campaign created: {campaign_id}")

    # Add leads (marks sheet every 10)
    print(f"\nAdding {len(leads_to_push)} leads to campaign (batches of 10)...")
    def col_letter(idx):
        if idx < 26: return chr(65 + idx)
        return chr(64 + idx // 26) + chr(65 + idx % 26)
    added_col = f"'{tab_name}'!" + col_letter(idx_added)
    instantly_leads = [lp["lead"] for lp in leads_to_push]
    added_count, failed_count = add_leads(
        api_key, campaign_id, instantly_leads,
        service=service, sheet_id=sheet_id,
        added_col=added_col, leads_to_push=leads_to_push,
    )

    # Summary
    print(f"\n{'='*50}")
    print(f"Campaign Push Complete")
    print(f"  Campaign: {args.campaign_name}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"  Leads added: {added_count}")
    if failed_count:
        print(f"  Leads failed: {failed_count}")
    print(f"  Sending accounts: {len(accounts)}")
    print(f"  Sheet updated: {len(leads_to_push)} rows marked")
    print(f"\n  Campaign created as DRAFT — activate in Instantly when ready.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
