"""
Phase 3a: Generate personalized outreach emails using Claude → Google Sheets

Reads lead data from the sheet, generates email copy per lead,
and writes to the Body column. Does NOT touch Instantly.
"""

import os
import sys
import json
import argparse
import time
import anthropic
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load .env from the skill's parent .claude directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

MAX_WORKERS = 5
MAX_RETRIES = 3

SYSTEM_PROMPT = "You are an amazing email copywriter for B2B outreach."

EMAIL_PROMPT = """You are writing outbound emails for a recruitment company in the HR recruitment space, specializing in placing HR Talents in the US.

Your role is to write a compelling email body to get in touch with a decision maker at a company actively hiring for HR talent.

You will be provided with: company name, job title they're hiring for, job description, job location, company description, and company industry.

Fill in the variables in this template and personalize it:

Noticed {{company}} posted a {{ROLE_TITLE}} role. Is it still open?

I'm connected with an HR recruitment firm — they have a {{LOCATION}} based {{SAME_ROLE_TITLE}} who just became available and would be perfect for the role. {{YEARS}}+ years as {{SAME_ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

Open to interviewing this week if you are still looking.

RULES:
1. Tone: casual bar conversation, very spartan. No fancy language.
2. SHORTEN the role title to how a recruiter would actually say it in conversation. Remove filler words, use common abbreviations. Examples:
   - "Global HR Business Partner - GTM" → "Global HRBP"
   - "Director of Academic Affairs HR Operations & Compensation" → "Dir. of HR Ops"
   - "Director, Human Resources Business Partner (HRBP)" → "HR Director"
   - "HR Payroll Coordinator (Remote GovCon / Union)" → "HR Payroll Coordinator"
   - "194 - Manager Human Resources" → "HR Manager"
   - "Talent Acquisition Systems Administrator" → "TA Systems Admin"
   Keep it precise but concise. Strip parentheticals, numbering prefixes, and redundant words.
3. Do not hallucinate locations. If remote or unclear, omit location.
4. No exclamation points. No em dashes. Use commas instead.
5. Abbreviate locations casually: "San Fran" not "San Francisco", "Philly" not "Philadelphia", "DC" not "Washington D.C."
6. CLEAN the company name. Strip legal suffixes, geographic tags, and generic words to get the casual version — how you'd say it in conversation. Examples:
   - "Servexo USA" → "Servexo"
   - "Marrakech Inc" → "Marrakech"
   - "Alpine Solutions Group" → "Alpine"
   - "The Pivot Group" → "Pivot Group"
   - "SSI Services" → "SSI"
   - "Century 21 Real Estate" → "Century 21"
   - "Keolis Commuter Services" → "Keolis"
   Remove: Inc, LLC, Corp, Ltd, Co., USA, Group, Services, Solutions, Realty, Real Estate, Holdings, International, Technologies, "The" prefix. Keep the core brand name that the person would recognize.
7. Keep proper capitalization for names and titles.
8. YEARS: use the years required in job description + 2. If not stated, estimate for the role level + 2.
9. SPECIALTY_1 and SPECIALTY_2: These are the MOST IMPORTANT part of the email. Read the job description carefully and find the 2 things that make this hire HARD TO FILL. What specific, niche requirement would make the hiring manager think "we've been struggling to find someone with exactly this"?
   - Look for: specific compliance requirements (multi-state, union, government), niche systems (Workday, SAP SuccessFactors, ADP), industry-specific experience, scale challenges (supporting 500+ employees, multi-site), certifications, or uncommon combinations.
   - BAD examples (too generic, will get ignored): "HR operations", "team management", "compliance", "payroll processing"
   - GOOD examples (specific pain points): "multi-state compliance across 15+ jurisdictions", "Workday HCM implementation", "union contract negotiations", "scaling HR ops from 50 to 500", "government contracting payroll with prevailing wage", "M&A due diligence and workforce integration"
   - If the job description is vague, infer from the company context what would be hard to find.
10. Limit output to 60 words max.
11. Respond in JSON only: {{"body": "the email body here"}}

INPUT:
Company: {company_name}
Role: {job_title}
Location: {job_location}
Industry: {company_industry}
Company Description: {company_description}
Job Description (first 1000 chars): {job_description}
"""


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


def split_name(full_name):
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def generate_email(client, lead, retry=0):
    """Generate email copy for a single lead using Claude."""
    prompt = EMAIL_PROMPT.format(
        company_name=lead["company_name"],
        job_title=lead["job_title"],
        job_location=lead["job_location"],
        company_industry=lead["company_industry"],
        company_description=lead["company_description"][:300],
        job_description=lead["job_description"][:1000],
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()

        # Strip markdown code blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        data = json.loads(text)
        body = data.get("body", "")

        # Clean em dashes → commas
        body = body.replace("—", ",").replace("–", ",")

        return body

    except anthropic.RateLimitError:
        if retry < MAX_RETRIES:
            wait = (2 ** retry) * 2
            time.sleep(wait)
            return generate_email(client, lead, retry + 1)
        return None
    except (json.JSONDecodeError, Exception) as e:
        if retry < MAX_RETRIES:
            time.sleep(1)
            return generate_email(client, lead, retry + 1)
        print(f"  Error for {lead['company_name']}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Generate outreach emails using Claude")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing email bodies")
    parser.add_argument("--preview", type=int, default=0, help="Preview N emails without writing to sheet")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Read all data
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Sheet1"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows found")
        sys.exit(0)

    headers = all_rows[0]

    # Find column indices dynamically
    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    idx_person = col_idx("person_name")
    idx_email = col_idx("email")
    idx_company = col_idx("company name")
    idx_title = col_idx("job_title")
    idx_location = col_idx("job_location")
    idx_description = col_idx("job_description")
    idx_industry = col_idx("company_industry")
    idx_comp_desc = col_idx("company_description")
    idx_body = col_idx("Body")
    idx_firstname = col_idx("First name")
    idx_lastname = col_idx("Last name")

    missing = []
    for name, idx in [("person_name", idx_person), ("email", idx_email),
                       ("company name", idx_company), ("job_title", idx_title),
                       ("Body", idx_body)]:
        if idx is None:
            missing.append(name)
    if missing:
        print(f"Error: Missing columns: {', '.join(missing)}")
        sys.exit(1)

    # Collect rows needing email generation
    leads = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        person_name = cell(idx_person)
        email = cell(idx_email)
        body = cell(idx_body)

        # Skip if no email (Phase 2 didn't find one) or no person name
        if not email or not person_name:
            continue

        # Skip if body already exists (unless --overwrite)
        if body and not args.overwrite:
            continue

        first_name, last_name = split_name(person_name)

        leads.append({
            "row_num": i,
            "person_name": person_name,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": cell(idx_company),
            "job_title": cell(idx_title),
            "job_location": cell(idx_location) or "",
            "job_description": cell(idx_description) or "",
            "company_industry": cell(idx_industry) or "",
            "company_description": cell(idx_comp_desc) or "",
        })

    if not leads:
        print("No leads need email generation")
        sys.exit(0)

    print(f"\nGenerating emails for {len(leads)} leads...\n")

    client = anthropic.Anthropic(api_key=api_key)
    results = []

    # Generate emails concurrently
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_lead = {
            executor.submit(generate_email, client, lead): lead
            for lead in leads
        }
        for future in as_completed(future_to_lead):
            lead = future_to_lead[future]
            body = future.result()
            if body:
                full_body = f"Hi {lead['first_name']},\n\n{body}"
                results.append({
                    "row_num": lead["row_num"],
                    "first_name": lead["first_name"],
                    "last_name": lead["last_name"],
                    "body": full_body,
                })
                print(f"  Row {lead['row_num']}: {lead['company_name']} — done")
            else:
                print(f"  Row {lead['row_num']}: {lead['company_name']} — FAILED")

    if not results:
        print("No emails generated")
        sys.exit(1)

    # Preview mode
    if args.preview > 0:
        print(f"\n{'='*50}")
        print(f"PREVIEW (first {min(args.preview, len(results))} emails):\n")
        for r in results[:args.preview]:
            print(f"Row {r['row_num']} — {r['first_name']}:")
            print(f"  {r['body']}\n")
            print(f"  Best,\n  Jude\n")
            print(f"{'='*50}")
        if args.preview < len(results):
            print(f"... and {len(results) - args.preview} more")
        print("\nRun without --preview to write to sheet.")
        sys.exit(0)

    # Write to sheet: First name, Last name, Body columns
    print(f"\nWriting {len(results)} emails to sheet...")
    firstname_col = chr(65 + idx_firstname) if idx_firstname < 26 else "A" + chr(65 + idx_firstname - 26)
    lastname_col = chr(65 + idx_lastname) if idx_lastname < 26 else "A" + chr(65 + idx_lastname - 26)
    body_col = chr(65 + idx_body) if idx_body < 26 else "A" + chr(65 + idx_body - 26)

    updates = []
    for r in results:
        updates.append({
            "range": f"{firstname_col}{r['row_num']}",
            "values": [[r["first_name"]]],
        })
        updates.append({
            "range": f"{lastname_col}{r['row_num']}",
            "values": [[r["last_name"]]],
        })
        updates.append({
            "range": f"{body_col}{r['row_num']}",
            "values": [[r["body"]]],
        })

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()

    # Summary
    print(f"\n{'='*50}")
    print(f"Email Generation Complete")
    print(f"  Emails generated: {len(results)}")
    print(f"  Failed: {len(leads) - len(results)}")
    print(f"  Review in sheet, then run push_campaign.py to send to Instantly")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
