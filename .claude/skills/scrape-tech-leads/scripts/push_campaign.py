"""
Phase 3b: Push leads from Google Sheets → Instantly campaigns

Creates 3 campaigns based on template_variant:
- Eric_Perm A → rows with template_variant = perm_a
- Eric_Perm B → rows with template_variant = perm_b
- Contractors → rows with template_variant = contract

Mirrors the Reyna_March_17th campaign structure (4-step sequence).
"""

import os
import sys
import json
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

# Campaign name → (template_variant, existing_campaign_id)
CAMPAIGN_MAP = {
    "Eric_Perm A": {"variant": "perm_a", "id": "2bf67ce2-e154-49ad-baad-b2e16faa28c8"},
    "Eric_Perm B": {"variant": "perm_b", "id": "6bc6309e-a6d9-4d10-8e70-b1b3f07ab866"},
    "Contractors": {"variant": "contract", "id": "16046db5-17e0-41e7-af73-2e2eb0543e42"},
}


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


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


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


def create_campaign(api_key, name):
    """Create campaign with 4-step sequence. Sending accounts to be set up manually."""
    payload = {
        "name": name,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "New schedule",
                    "timing": {"from": "09:00", "to": "18:00"},
                    "days": {"1": True, "2": True, "3": True, "4": True, "5": True, "6": True},
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
                                "subject": "\u200bSanity check just in case",
                                "body": "<div>{{personalization}}\u00a0<br /><br />Worth an intro?<br /><br />Best,<br />Jude<br /><br />Sent from my iPhone<br /><br /><br /></div>",
                            }
                        ],
                    },
                    {
                        "type": "email",
                        "delay": 2,
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
                        "delay": 2,
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
    """Shorten job title for follow-up {{Role}} variable."""
    import re
    title = (job_title or "").strip()
    title = re.sub(r'\s*\(.*?\)', '', title)
    title = re.sub(r'^\d+\s*[-–]\s*', '', title)
    title = re.sub(r'\s*[-–]\s*\w+$', '', title)
    title = title.strip(" -–,")
    return title


def add_leads_to_campaign(api_key, campaign_id, leads, service, sheet_id, tab_indices):
    """Add leads to campaign one at a time, writing to sheet every 10."""
    added = 0
    failed = 0
    BATCH_SIZE = 10

    for i, lead_data in enumerate(leads):
        lead_payload = lead_data["lead"]
        lead_payload["campaign"] = campaign_id
        try:
            resp = requests.post(
                f"{INSTANTLY_BASE}/leads",
                headers=instantly_headers(api_key),
                json=lead_payload,
                timeout=30,
            )
            if resp.status_code == 200:
                added += 1
            else:
                print(f"    Lead {i+1} failed ({resp.status_code}): {resp.text[:200]}")
                failed += 1
        except requests.exceptions.RequestException as e:
            print(f"    Lead {i+1} error: {e}")
            failed += 1

        # Every 10 leads, mark them in the sheet
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            batch_start = (i // BATCH_SIZE) * BATCH_SIZE
            updates = []
            for ld in leads[batch_start:i + 1]:
                tab = ld["tab"]
                idx_added = tab_indices[tab]["added"]
                if idx_added is not None:
                    updates.append({
                        "range": f"'{tab}'!{col_letter(idx_added)}{ld['row_num']}",
                        "values": [["TRUE"]],
                    })
            if updates:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
            print(f"    Progress: {i+1}/{len(leads)} ({added} added, {failed} failed) → written to sheet")

    return added, failed


def read_tab_data(service, sheet_id, tab_name):
    """Read all rows from a tab."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return [], []
    return all_rows[0], all_rows


def main():
    parser = argparse.ArgumentParser(description="Push tech leads to Instantly campaign")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--tab", default="Data",
                        help="Tab to process (default: Data), or comma-separated names")
    parser.add_argument("--campaign_name", default=None, help="Name for new campaign (creates one if no --campaign_id)")
    parser.add_argument("--campaign_id", default=None, help="Existing campaign ID to push to")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to push (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview without pushing")
    args = parser.parse_args()

    api_key = os.getenv("INSTANTLY_API_KEY")
    if not api_key:
        print("Error: INSTANTLY_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Determine tabs
    tabs_to_process = [t.strip() for t in args.tab.split(",") if t.strip()]

    # Collect all leads
    all_leads = []
    tab_indices = {}

    for tab_name in tabs_to_process:
        print(f"\nReading '{tab_name}' tab...")
        headers, all_rows = read_tab_data(service, sheet_id, tab_name)
        if not headers:
            print(f"  No data found")
            continue

        def col_idx(name):
            try:
                return headers.index(name)
            except ValueError:
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
        idx_variant = col_idx("template_variant")
        idx_cleaned_role = col_idx("cleaned_role")

        missing = []
        for name, idx in [("email", idx_email), ("Body", idx_body),
                           ("Added to instantly", idx_added), ("template_variant", idx_variant)]:
            if idx is None:
                missing.append(name)
        if missing:
            print(f"  Missing columns: {', '.join(missing)}")
            continue

        tab_indices[tab_name] = {"added": idx_added}

        tab_count = 0
        for i, row in enumerate(all_rows[1:], start=2):
            def cell(idx):
                if idx is None:
                    return ""
                return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

            email_addr = cell(idx_email)
            body = cell(idx_body)
            added = cell(idx_added)
            variant = cell(idx_variant)

            if not email_addr or email_addr == "not_found" or not body or added:
                continue

            first_name = cell(idx_firstname)
            last_name = cell(idx_lastname)
            company = cell(idx_company)
            job_title = cell(idx_title)
            role = cell(idx_cleaned_role) if idx_cleaned_role is not None else job_title
            job_link = cell(idx_url)
            company_linkedin = cell(idx_company_linkedin)
            linkedin_url = cell(idx_linkedin)
            dm_title = cell(idx_result_title)

            lead_data = {
                "tab": tab_name,
                "row_num": i,
                "variant": variant,
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
                        "Company_Linkedin": company_linkedin,
                        "Decision Maker Title": dm_title,
                    },
                },
            }

            all_leads.append(lead_data)
            tab_count += 1

            if args.limit and len(all_leads) >= args.limit:
                break

        print(f"  {tab_count} leads ready to push")

    if not all_leads:
        print("\nNo leads ready to push (need email + Body, not already added)")
        sys.exit(0)

    print(f"\nTotal: {len(all_leads)} leads")

    # Dry run
    if args.dry_run:
        print(f"\nDRY RUN — would push {len(all_leads)} leads:\n")
        for ld in all_leads[:10]:
            l = ld["lead"]
            print(f"  {l['first_name']} {l['last_name']} <{l['email']}> — {l['custom_variables']['Role']}")
        if len(all_leads) > 10:
            print(f"  ... and {len(all_leads) - 10} more")
        sys.exit(0)

    # Get or create campaign
    campaign_id = args.campaign_id
    if not campaign_id:
        campaign_name = args.campaign_name or "Tech Recruitment"
        print(f"Creating campaign '{campaign_name}'...")
        campaign_id = create_campaign(api_key, campaign_name)
        if not campaign_id:
            print("Error: Failed to create campaign")
            sys.exit(1)
        print(f"  Campaign created: {campaign_id}")
    else:
        print(f"\nUsing existing campaign: {campaign_id}")

    # Push leads
    print(f"\nAdding {len(all_leads)} leads to campaign...")
    added, failed = add_leads_to_campaign(
        api_key, campaign_id, all_leads, service, sheet_id, tab_indices
    )

    # Final summary
    print(f"\n{'='*50}")
    print(f"Campaign Push Complete")
    print(f"  Campaign: {campaign_id}")
    print(f"  Leads added: {added}")
    print(f"  Failed: {failed}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
