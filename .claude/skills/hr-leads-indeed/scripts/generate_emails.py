"""
Phase 4: Generate personalized outreach emails using Claude → Google Sheets

Reads lead data from the sheet, generates email copy per lead,
and writes First Name, Last Name, Email Body columns.
Does NOT push to Instantly — that's push_campaign.py's job.

IMPORTANT: The SKILL.md requires user approval on templates before running.
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
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

MAX_WORKERS = 5
MAX_RETRIES = 3
BATCH_SIZE = 10
SHEET_WRITE_DELAY = 1.5

SYSTEM_PROMPT = "You are an amazing email copywriter for B2B outreach."

# Column indices (matching pull_dataset.py HEADERS)
COL_JOB_TITLE = 1        # B
COL_OCCUPATIONS = 3       # D
COL_JOB_DESCRIPTION = 9   # J
COL_COMPANY_NAME = 10     # K
COL_COMPANY_WEBSITE = 11  # L
COL_COMPANY_DESC = 15     # P
COL_CITY = 17             # R
COL_STATE = 18            # S
COL_DM_NAME = 19          # T
COL_EMAIL = 22            # W
COL_FIRST_NAME = 23       # X
COL_LAST_NAME = 24        # Y
COL_EMAIL_BODY = 25       # Z
COL_ADDED_INSTANTLY = 26  # AA

SHARED_RULES = """
RULES:
1. Tone: casual bar conversation, very spartan. No fancy language. When listing alternatives use slashes not "or" — "ADP/Paylocity" not "ADP or Paylocity", "FMLA/ADA" not "FMLA or ADA".
2. REWRITE the role title the way a recruiter would actually say it out loud in a casual conversation. Not just shortened — rewritten so it sounds natural and human. The test: if you'd never say it that way to a friend, rewrite it.
   - "Global HR Business Partner - GTM" → "Global HRBP"
   - "Director of Academic Affairs HR Operations & Compensation" → "HR Ops Director"
   - "Director, Human Resources Business Partner (HRBP)" → "HR Director"
   - "HR Payroll Coordinator (Remote GovCon / Union)" → "Payroll Coordinator"
   - "194 - Manager Human Resources" → "HR Manager"
   - "Talent Acquisition Systems Administrator" → "TA Systems Admin"
   - "Director Talent Acquisition" → "TA Director" (NOT "Director TA")
   - "Corporate Recruiter (Sales | Supply Chain)" → "Corporate Recruiter"
   - "Immigration & Mobility Specialist" → "Immigration Specialist"
   - "Senior Manager, People Operations" → "People Ops Manager"
   Key rule: the job function goes LAST, the level/seniority goes FIRST. "HR Director" not "Director HR". "TA Manager" not "Manager TA". Strip parentheticals, numbering prefixes, pipe-separated lists, and redundant words.
3. ROLE_PLURAL: pluralize the rewritten role title naturally. The plural must also sound like something a human would say.
   - "HR Manager" → "HR Managers"
   - "TA Director" → "TA Directors"
   - "HRBP" → "HRBPs"
   - "Payroll Coordinator" → "Payroll Coordinators"
   - "Corporate Recruiter" → "Corporate Recruiters"
4. Do not hallucinate locations. If remote or unclear, omit location entirely (remove "{{LOCATION}} based" from the sentence).
5. No exclamation points. No em dashes. Use commas instead.
6. SHORTEN city names whenever possible — use the name people actually say in conversation:
   "San Francisco" → "San Fran", "Philadelphia" → "Philly", "Washington D.C." → "DC", "Los Angeles" → "LA", "San Antonio" → "San Antonio", "New York" → "NYC", "Las Vegas" → "Vegas", "Minneapolis" → "Minneapolis", "Charlotte, NC" → "Charlotte", "Indianapolis" → "Indy". Drop state suffixes (", NC", ", TX", etc.).
7. CLEAN and SHORTEN the company name to the casual version — how you'd actually say it in conversation. Strip legal suffixes, geographic tags, generic descriptors, and anything that makes it sound like a legal filing instead of a name. Examples:
   - "Servexo USA" → "Servexo"
   - "Marrakech Inc" → "Marrakech"
   - "Alpine Solutions Group" → "Alpine"
   - "The Pivot Group" → "Pivot Group"
   - "SSI Services" → "SSI"
   - "Century 21 Real Estate" → "Century 21"
   - "Keolis Commuter Services" → "Keolis"
   - "Walnut Cove Health and Rehabilitation" → "Walnut Cove"
   - "Weld county School district RE-8" → "Weld RE-8"
   - "City of Wilmington, NC" → "City of Wilmington"
   - "North America Security & Select Services" → "North America Security"
   - "Alternative Nursing ServicesServices, Inc." → "Alternative Nursing"
   - "Oglebay Resort & Conference Center" → "Oglebay"
   Remove: Inc, LLC, Corp, Ltd, Co., USA, Group, Services, Solutions, Realty, Real Estate, Holdings, International, Technologies, "The" prefix, state/country suffixes, industry descriptors (Health and Rehabilitation, Conference Center, etc.), district codes. Keep only the core brand name a person would recognize.
8. Keep proper capitalization for names and titles.
9. SPECIALTY_1 and SPECIALTY_2: Pick 2 things from the JD that require specialized certification, specific software, or industry-specific regulation. If none exist, pick the most niche technical skill mentioned. Never pick something generic that any HR professional would have.
10. ALWAYS keep the 3-paragraph structure with line breaks between them. Never collapse into one block.
11. ALWAYS use the full ROLE_PLURAL consistently — never drop it or shorten further in the last sentence.
12. Limit output to 65 words max.
13. Respond in JSON only: {{"body": "the email body here", "role": "the cleaned singular role title you used"}}. Use \\n\\n for paragraph breaks in the JSON string.

INPUT:
Company: {company_name}
Role: {job_title}
Location: {job_location}
Industry: {company_industry}
Company Description: {company_description}
Job Description: {job_description}
{opus_specialties}"""

RULES_B = """
RULES:
1. ONLY fill in {{COMPANY}} and {{ROLE}}. Do NOT add any extra sentences, CTAs, questions, or sign-offs. Output the template EXACTLY as written with only those two variables replaced.
2. REWRITE the role title the way a recruiter would actually say it out loud in a casual conversation. Not just shortened — rewritten so it sounds natural and human. The test: if you'd never say it that way to a friend, rewrite it.
   - "Global HR Business Partner - GTM" → "Global HRBP"
   - "Director of Academic Affairs HR Operations & Compensation" → "HR Ops Director"
   - "Director, Human Resources Business Partner (HRBP)" → "HR Director"
   - "HR Payroll Coordinator (Remote GovCon / Union)" → "Payroll Coordinator"
   - "194 - Manager Human Resources" → "HR Manager"
   - "Director Talent Acquisition" → "TA Director" (NOT "Director TA")
   Key rule: the job function goes LAST, the level/seniority goes FIRST. "HR Director" not "Director HR". Strip parentheticals, numbering prefixes, pipe-separated lists, and redundant words.
3. CLEAN and SHORTEN the company name to the casual version — how you'd actually say it in conversation. Strip legal suffixes, geographic tags, generic descriptors, and anything that sounds like a legal filing.
   - "Walnut Cove Health and Rehabilitation" → "Walnut Cove"
   - "Weld county School district RE-8" → "Weld RE-8"
   - "City of Wilmington, NC" → "City of Wilmington"
   - "Alternative Nursing ServicesServices, Inc." → "Alternative Nursing"
   Remove: Inc, LLC, Corp, Ltd, Co., USA, Group, Services, Solutions, Realty, Real Estate, Holdings, International, Technologies, "The" prefix, state/country suffixes, industry descriptors. Keep only the core brand name.
4. No exclamation points. No em dashes. Use commas instead.
5. Keep proper capitalization for names and titles.
6. Respond in JSON only: {{"body": "the email body here", "role": "the cleaned singular role title you used"}}. Use \\n\\n for paragraph breaks in the JSON string.

INPUT:
Company: {company_name}
Role: {job_title}
"""

PREAMBLE = """You are writing outbound emails for a recruitment company in the HR recruitment space, specializing in placing HR Talents in the US.

Your role is to write a compelling email body to get in touch with a decision maker at a company actively hiring for HR talent.

You will be provided with: company name, job title they're hiring for, job description, job location, company description, and company industry.

Fill in the variables in this template and personalize it:

"""

# Default templates — can be overridden via --template_a / --template_b
DEFAULT_TEMPLATE_A = """Noticed {{COMPANY}} posted a {{ROLE}} role on Indeed. Is this hire a priority in the next 15-30 days?

Asking because I'm partnered with TalentCount, a recruitment firm that focuses exclusively on HR and has filled similar HR roles for industry leaders like Mercedes-Benz.

They're already connected to a few pre-vetted {{LOCATION}}-based candidates with {{SPECIALTY_1}} and {{SPECIALTY_2}} experience ready to go."""

DEFAULT_TEMPLATE_B = """Saw {{COMPANY}}'s opening for the {{ROLE}} role on Indeed.

I might be off base, but I'd guess most of this is being handled through internal recruiting and inbound applicants, which can make the hiring timeline harder to predict.

I'm partnered with TalentCount. They specialize exclusively in HR and have filled similar {{ROLE}} searches for industry leaders like Mercedes-Benz. Their average time-to-fill is 3-4 weeks because they're connected with pre-vetted passive candidates who aren't on job boards."""


SENIOR_ROLE_KEYWORDS = [
    "director", "vp ", "v.p.", "vice president", "head of", "chief",
    "chro", "cpo", "svp", "evp", "coo", "partner", "president",
]

SPECIALTY_PROMPT = """Read this job description and return the 2 most specific, niche requirements that make this role genuinely hard to fill. Pick things that require specialized certification, specific software, or industry-specific regulation. Never pick something generic that any HR professional would have.

Company: {company_name}
Role: {job_title}
Industry: {company_industry}
Job Description: {job_description}

Respond in JSON only: {{"specialty_1": "...", "specialty_2": "..."}}"""


def col_letter(idx):
    """Convert 0-based column index to sheet letter (0=A, 25=Z, 26=AA, etc.)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def split_name(full_name):
    name = full_name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0], parts[0]
    parts = name.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def is_senior_role(job_title):
    t = (job_title or "").lower()
    return any(kw in t for kw in SENIOR_ROLE_KEYWORDS)


def extract_specialties(client, lead):
    """Use Opus to extract niche specialties from the JD."""
    prompt = SPECIALTY_PROMPT.format(
        company_name=lead["company_name"],
        job_title=lead["job_title"],
        company_industry=lead["company_industry"],
        job_description=lead["job_description"],
    )
    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        data = json.loads(text)
        return data.get("specialty_1", ""), data.get("specialty_2", "")
    except Exception:
        return "", ""


def generate_email(client, lead, template_a_text, template_b_text, retry=0):
    """Generate email copy for a single lead using Claude.
    Senior job titles → Template B (Sonnet only). All others → Template A (Opus specialties + Sonnet email)."""
    variant = "B" if is_senior_role(lead.get("job_title", "")) else "A"

    if variant == "B":
        prompt = PREAMBLE + template_b_text + RULES_B.format(
            company_name=lead["company_name"],
            job_title=lead["job_title"],
        )
    else:
        # Use Opus to extract specialties first
        s1, s2 = extract_specialties(client, lead)
        opus_specialties = ""
        if s1 and s2:
            opus_specialties = f"\nUse these exact specialties (already extracted): SPECIALTY_1={s1}, SPECIALTY_2={s2}\n"

        location = lead.get("job_location", "")
        prompt = PREAMBLE + template_a_text + SHARED_RULES.format(
            company_name=lead["company_name"],
            job_title=lead["job_title"],
            job_location=location,
            company_industry=lead.get("company_industry", ""),
            company_description=lead.get("company_description", "")[:300],
            job_description=lead.get("job_description", ""),
            opus_specialties=opus_specialties,
        )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        data = json.loads(text)
        body = data.get("body", "")
        role = data.get("role", "")

        # Clean em dashes → commas
        body = body.replace("\u2014", ",").replace("\u2013", ",")

        return {"body": body, "role": role, "variant": variant}

    except anthropic.RateLimitError:
        if retry < MAX_RETRIES:
            wait = (2 ** retry) * 2
            time.sleep(wait)
            return generate_email(client, lead, template_a_text, template_b_text, retry + 1)
        return None
    except (json.JSONDecodeError, Exception) as e:
        if retry < MAX_RETRIES:
            time.sleep(1)
            return generate_email(client, lead, template_a_text, template_b_text, retry + 1)
        print(f"  Error for {lead['company_name']}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Generate outreach emails using Claude")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing email bodies")
    parser.add_argument("--limit", type=int, default=0, help="Max leads (0 = all)")
    parser.add_argument("--preview", type=int, default=0, help="Preview N emails without writing")
    parser.add_argument("--template_a", help="Path to Template A text file (overrides default)")
    parser.add_argument("--template_b", help="Path to Template B text file (overrides default)")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        return

    # Load templates
    template_a_text = DEFAULT_TEMPLATE_A
    template_b_text = DEFAULT_TEMPLATE_B
    if args.template_a:
        with open(args.template_a) as f:
            template_a_text = f.read().strip()
        print(f"  Loaded Template A from: {args.template_a}")
    if args.template_b:
        with open(args.template_b) as f:
            template_b_text = f.read().strip()
        print(f"  Loaded Template B from: {args.template_b}")

    print("=== Generate Emails ===")
    print(f"  Template A (non-senior): Opus specialties + Sonnet email")
    print(f"  Template B (senior): Sonnet only (company + role)\n")

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    # Detect tab
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"  Using tab: '{tab_name}'")

    # Read sheet
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("  No data rows found.")
        return

    # Collect leads needing email generation
    leads = []
    for i, row in enumerate(all_rows[1:]):
        if args.limit > 0 and len(leads) >= args.limit:
            break

        dm_name = cell(row, COL_DM_NAME)
        email = cell(row, COL_EMAIL)
        email_body = cell(row, COL_EMAIL_BODY)

        # Need DM name + email, skip if body already exists (unless --overwrite)
        if not dm_name or not email:
            continue
        if email_body and not args.overwrite:
            continue

        company_name = cell(row, COL_COMPANY_NAME)
        job_title = cell(row, COL_JOB_TITLE)
        city = cell(row, COL_CITY)
        state = cell(row, COL_STATE)
        job_location = f"{city}, {state}" if city and state else city or state or ""

        leads.append({
            "sheet_row": i + 2,
            "dm_name": dm_name,
            "email": email,
            "company_name": company_name,
            "job_title": job_title,
            "job_location": job_location,
            "job_description": cell(row, COL_JOB_DESCRIPTION),
            "company_industry": cell(row, COL_OCCUPATIONS),
            "company_description": cell(row, COL_COMPANY_DESC),
        })

    print(f"  {len(leads)} leads need email generation")
    if not leads:
        return

    client = anthropic.Anthropic(api_key=api_key)
    total_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"  Processing in {total_batches} batches of {BATCH_SIZE}\n")

    results = []
    failed = 0

    for b in range(total_batches):
        batch = leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        batch_results = []
        print(f"  Batch {b + 1}/{total_batches}")

        if args.preview > 0 and len(results) >= args.preview:
            break

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(generate_email, client, lead, template_a_text, template_b_text): lead
                for lead in batch
            }
            for future in as_completed(futures):
                lead = futures[future]
                result = future.result()
                if result:
                    first_name, last_name = split_name(lead["dm_name"])
                    full_body = f"Hey {first_name},\n\n{result['body']}"
                    r = {
                        "sheet_row": lead["sheet_row"],
                        "first_name": first_name,
                        "last_name": last_name,
                        "body": full_body,
                        "role": result["role"],
                        "variant": result["variant"],
                        "company_name": lead["company_name"],
                    }
                    batch_results.append(r)
                    results.append(r)
                    print(f"    Row {lead['sheet_row']}: {lead['company_name']} [{result['variant']}] — done")
                else:
                    print(f"    Row {lead['sheet_row']}: {lead['company_name']} — FAILED")
                    failed += 1

        # Write batch to sheet (unless preview mode)
        if batch_results and args.preview == 0:
            updates = []
            for r in batch_results:
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_FIRST_NAME)}{r['sheet_row']}",
                    "values": [[r["first_name"]]],
                })
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_LAST_NAME)}{r['sheet_row']}",
                    "values": [[r["last_name"]]],
                })
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}{r['sheet_row']}",
                    "values": [[r["body"]]],
                })

            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"    → Written {len(batch_results)} emails to sheet")

        if b + 1 < total_batches:
            time.sleep(SHEET_WRITE_DELAY)

    # Preview mode
    if args.preview > 0:
        print(f"\n{'='*50}")
        print(f"PREVIEW (first {min(args.preview, len(results))} emails):\n")
        for r in results[:args.preview]:
            print(f"Row {r['sheet_row']} — {r['company_name']} [{r['variant']}]:")
            print(f"  {r['body']}\n")
            print(f"  Best,\n  Jude\n")
            print(f"{'='*50}")
        if args.preview < len(results):
            print(f"... and {len(results) - args.preview} more")
        print("\nRun without --preview to write to sheet.")
        return

    print(f"\n{'='*50}")
    print(f"Email Generation Complete")
    print(f"  Emails generated: {len(results)}")
    print(f"  Failed: {failed}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
