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


def create_campaign(api_key, name, sending_accounts):
    """Create campaign matching Reyna_March_17th structure (4-step sequence)."""
    payload = {
        "name": name,
        "email_list": sending_accounts,
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
    parser = argparse.ArgumentParser(description="Push tech leads to Instantly campaigns (3 campaigns by template)")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--tab", default="both",
                        help="Tab(s) to process: 'perm', 'contract', 'both', or comma-separated custom names")
    parser.add_argument("--dry_run", action="store_true", help="Preview without creating campaigns")
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
    tab_arg = args.tab.strip().lower()
    if tab_arg == "both":
        tabs_to_process = ["Perm", "Contract"]
    elif tab_arg == "perm":
        tabs_to_process = ["Perm"]
    elif tab_arg == "contract":
        tabs_to_process = ["Contract"]
    else:
        tabs_to_process = [t.strip() for t in args.tab.split(",") if t.strip()]

    # Collect leads from all tabs, grouped by template_variant
    leads_by_variant = {"perm_a": [], "perm_b": [], "contract": []}
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

            if not email_addr or email_addr == "not_found" or not body or added or not variant:
                continue

            first_name = cell(idx_firstname)
            last_name = cell(idx_lastname)
            company = cell(idx_company)
            job_title = cell(idx_title)
            role = shorten_role(job_title)
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

            if variant in leads_by_variant:
                leads_by_variant[variant].append(lead_data)
                tab_count += 1

        print(f"  {tab_count} leads ready to push")

    # Summary
    total = sum(len(v) for v in leads_by_variant.values())
    if total == 0:
        print("\nNo leads ready to push (need email + Body + template_variant, not already added)")
        sys.exit(0)

    print(f"\n{'='*50}")
    for campaign_name, info in CAMPAIGN_MAP.items():
        count = len(leads_by_variant[info["variant"]])
        print(f"  {campaign_name}: {count} leads ({info['variant']})")
    print(f"  Total: {total}")
    print(f"{'='*50}")

    # Dry run
    if args.dry_run:
        print(f"\nDRY RUN — would push to existing campaigns:\n")
        for campaign_name, info in CAMPAIGN_MAP.items():
            leads = leads_by_variant[info["variant"]]
            print(f"  {campaign_name} [{info['id'][:8]}...] ({len(leads)} leads):")
            for ld in leads[:3]:
                l = ld["lead"]
                print(f"    {l['first_name']} {l['last_name']} <{l['email']}> — {l['custom_variables']['Role']}")
            if len(leads) > 3:
                print(f"    ... and {len(leads) - 3} more")
            print()
        sys.exit(0)

    # Push leads to existing campaigns
    for campaign_name, info in CAMPAIGN_MAP.items():
        variant = info["variant"]
        campaign_id = info["id"]
        leads = leads_by_variant[variant]
        if not leads:
            print(f"\n  Skipping {campaign_name} — no leads")
            continue

        print(f"\n--- {campaign_name} ({len(leads)} leads) → {campaign_id[:8]}... ---")
        print(f"  Adding {len(leads)} leads...")
        added, failed = add_leads_to_campaign(
            api_key, campaign_id, leads, service, sheet_id, tab_indices
        )
        print(f"  Done: {added} added, {failed} failed")

    # Final summary
    print(f"\n{'='*50}")
    print(f"Campaign Push Complete")
    for campaign_name, info in CAMPAIGN_MAP.items():
        count = len(leads_by_variant[info["variant"]])
        if count:
            print(f"  {campaign_name}: {count} leads → {info['id'][:8]}...")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
