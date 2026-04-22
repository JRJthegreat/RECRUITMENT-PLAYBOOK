"""
Phase 3a: Generate personalized outreach emails using Claude → Google Sheets

Reads lead data from the sheet, generates email copy per lead,
and writes First name, Last name, Body, and cleaned_role columns.
Does NOT push to Instantly — that's push_campaign.py's job.
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

PREAMBLE = """You are writing outbound emails for a recruitment company in the HR recruitment space, specializing in placing HR Talents in the US.

Your role is to write a compelling email body to get in touch with a decision maker at a company actively hiring for HR talent.

You will be provided with: company name, job title they're hiring for, job description, job location, company description, and company industry.

Fill in the variables in this template and personalize it:

"""

EMAIL_PROMPT_A = PREAMBLE + """Noticed {{COMPANY}} posted a {{ROLE}} role on Indeed. Is this hire a priority in the next 15-30 days?

Asking because I'm partnered with TalentCount, a recruitment firm that focuses exclusively on HR and has filled similar HR roles for industry leaders like Mercedes-Benz.

They're already connected to a few pre-vetted {{LOCATION}}-based candidates with {{SPECIALTY_1}} and {{SPECIALTY_2}} experience ready to go.
""" + SHARED_RULES

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

EMAIL_PROMPT_B = PREAMBLE + """Saw {{COMPANY}}'s opening for the {{ROLE}} role on Indeed.

I might be off base, but I'd guess most of this is being handled through internal recruiting and inbound applicants, which can make the hiring timeline harder to predict.

I'm partnered with TalentCount. They specialize exclusively in HR and have filled similar {{ROLE}} searches for industry leaders like Mercedes-Benz. Their average time-to-fill is 3-4 weeks because they're connected with pre-vetted passive candidates who aren't on job boards.
""" + RULES_B

# Keep EMAIL_PROMPT as alias for backward-compat
EMAIL_PROMPT = EMAIL_PROMPT_A


def col_letter(idx):
    """Convert 0-based column index to sheet letter (0=A, 25=Z, 26=AA, etc.)."""
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


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
    name = full_name.strip()
    # Handle "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0], parts[0]  # First from after comma, Last from before
    parts = name.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


SENIOR_ROLE_KEYWORDS = [
    "director", "vp ", "v.p.", "vice president", "head of", "chief",
    "chro", "cpo", "svp", "evp", "coo", "partner", "president",
]

def is_senior_role(job_title):
    t = (job_title or "").lower()
    return any(kw in t for kw in SENIOR_ROLE_KEYWORDS)


SPECIALTY_PROMPT = """Read this job description and return the 2 most specific, niche requirements that make this role genuinely hard to fill. Pick things that require specialized certification, specific software, or industry-specific regulation. Never pick something generic that any HR professional would have.

Company: {company_name}
Role: {job_title}
Industry: {company_industry}
Job Description: {job_description}

Respond in JSON only: {{"specialty_1": "...", "specialty_2": "..."}}"""


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


def generate_email(client, lead, retry=0):
    """Generate email copy for a single lead using Claude.
    Senior job titles → Template B (Sonnet only). All others → Template A (Opus for specialties + Sonnet for email)."""
    variant = lead.get("variant") or ("B" if is_senior_role(lead.get("job_title", "")) else "A")
    template = EMAIL_PROMPT_B if variant == "B" else EMAIL_PROMPT_A
    fmt_args = dict(
        company_name=lead["company_name"],
        job_title=lead["job_title"],
    )
    if variant == "A":
        # Use Opus to extract specialties first
        s1, s2 = extract_specialties(client, lead)
        # Pass specialties as context so Sonnet uses them directly
        fmt_args.update(
            job_location=lead["job_location"],
            company_industry=lead["company_industry"],
            company_description=lead["company_description"][:300],
            job_description=lead["job_description"],
        )
        if s1 and s2:
            fmt_args["opus_specialties"] = f"\nUse these exact specialties (already extracted): SPECIALTY_1={s1}, SPECIALTY_2={s2}\n"
        else:
            fmt_args["opus_specialties"] = ""
    prompt = template.format(**fmt_args
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
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
        role = data.get("role", "")

        # Clean em dashes → commas
        body = body.replace("—", ",").replace("–", ",")

        return {"body": body, "role": role, "variant": variant}

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
    parser.add_argument("--limit", type=int, default=0, help="Max leads to generate emails for (0 = all)")
    parser.add_argument("--preview", type=int, default=0, help="Preview N emails without writing to sheet")
    args = parser.parse_args()

    print("Templates: A (urgent/placed) and B (priority/exclusive) — 50/50 random split per lead.\n")

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

    # Detect tab — try "Data" first (standard pipeline), fall back to "Leads" (Apify import)
    tab_name = "Data"
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if "Data" not in tab_titles and "Leads" in tab_titles:
        tab_name = "Leads"
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

    # Column aliases — maps canonical name → list of alternates to try
    COLUMN_ALIASES = {
        "person_name":        ["person_name", "DM Name"],
        "email":              ["email", "Email"],
        "company name":       ["company name", "Company Name"],
        "job_title":          ["job_title", "Job Title"],
        "job_location":       ["job_location", "City"],
        "job_description":    ["job_description", "Job Description"],
        "company_industry":   ["company_industry", "Occupations"],
        "company_description":["company_description", "Company Description"],
        "Body":               ["Body", "Email Body"],
        "First name":         ["First name", "First Name"],
        "Last name":          ["Last name", "Last Name"],
        "template_variant":   ["template_variant"],
    }

    def col_idx(canonical):
        for alias in COLUMN_ALIASES.get(canonical, [canonical]):
            try:
                return headers.index(alias)
            except ValueError:
                continue
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
    idx_variant = col_idx("template_variant")

    # Get the sheet's grid properties so we know how many columns exist
    sheet_props = next(
        s["properties"] for s in meta.get("sheets", [])
        if s["properties"]["title"] == tab_name
    )
    sheet_gid = sheet_props["sheetId"]
    current_col_count = sheet_props["gridProperties"]["columnCount"]

    def ensure_column(canonical, label):
        nonlocal current_col_count
        idx = col_idx(canonical)
        if idx is not None:
            return idx
        # Need to append a column if the sheet is at max
        new_idx = len(headers)
        if new_idx >= current_col_count:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"appendDimension": {
                    "sheetId": sheet_gid,
                    "dimension": "COLUMNS",
                    "length": 1,
                }}]},
            ).execute()
            current_col_count += 1
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(new_idx)}1",
            valueInputOption="RAW",
            body={"values": [[label]]},
        ).execute()
        headers.append(label)
        print(f"  Added '{label}' column at {col_letter(new_idx)}")
        return new_idx

    idx_cleaned_role = ensure_column("cleaned_role", "cleaned_role")
    idx_variant = ensure_column("template_variant", "template_variant")

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
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": cell(idx_company),
            "job_title": cell(idx_title),
            "job_location": cell(idx_location) or "",
            "job_description": cell(idx_description) or "",
            "company_industry": cell(idx_industry) or "",
            "company_description": cell(idx_comp_desc) or "",
        })

        if args.limit and len(leads) >= args.limit:
            break

    if not leads:
        print("No leads need email generation")
        sys.exit(0)

    BATCH_SIZE = 10
    total_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nGenerating emails for {len(leads)} leads in batches of {BATCH_SIZE} ({total_batches} batches)...\n")

    client = anthropic.Anthropic(api_key=api_key)
    results = []
    failed = 0

    firstname_col = col_letter(idx_firstname)
    lastname_col = col_letter(idx_lastname)
    body_col = col_letter(idx_body)
    role_col = col_letter(idx_cleaned_role)

    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch = leads[batch_start:batch_start + BATCH_SIZE]
        batch_results = []

        print(f"  Batch {batch_num + 1}/{total_batches}")

        # Preview mode: just generate and print, don't write
        if args.preview > 0 and len(results) >= args.preview:
            break

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_lead = {
                executor.submit(generate_email, client, lead): lead
                for lead in batch
            }
            for future in as_completed(future_to_lead):
                lead = future_to_lead[future]
                result = future.result()
                if result:
                    body = result["body"] if isinstance(result, dict) else result
                    role = result.get("role", "") if isinstance(result, dict) else ""
                    variant = result.get("variant", "") if isinstance(result, dict) else ""
                    full_body = f"Hey {lead['first_name']},\n\n{body}"
                    r = {
                        "row_num": lead["row_num"],
                        "email": lead["email"],
                        "first_name": lead["first_name"],
                        "last_name": lead["last_name"],
                        "body": full_body,
                        "role": role,
                        "variant": variant,
                        "company_name": lead["company_name"],
                    }
                    batch_results.append(r)
                    results.append(r)
                    print(f"    Row {lead['row_num']}: {lead['company_name']} — done")
                else:
                    print(f"    Row {lead['row_num']}: {lead['company_name']} — FAILED")
                    failed += 1

        # Write batch to sheet immediately (unless preview mode)
        if batch_results and args.preview == 0:
            updates = []
            for r in batch_results:
                updates.append({"range": f"'{tab_name}'!{firstname_col}{r['row_num']}", "values": [[r["first_name"]]]})
                updates.append({"range": f"'{tab_name}'!{lastname_col}{r['row_num']}", "values": [[r["last_name"]]]})
                updates.append({"range": f"'{tab_name}'!{body_col}{r['row_num']}", "values": [[r["body"]]]})
                if r.get("role"):
                    updates.append({"range": f"'{tab_name}'!{role_col}{r['row_num']}", "values": [[r["role"]]]})
                if r.get("variant"):
                    updates.append({"range": f"'{tab_name}'!{col_letter(idx_variant)}{r['row_num']}", "values": [[r["variant"]]]})

            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"    → Written to sheet. Running total: {len(results)} done, {failed} failed")

            # 1.5s delay between batches to stay under 60 writes/min
            if batch_num + 1 < total_batches:
                time.sleep(1.5)

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

    # Summary
    print(f"\n{'='*50}")
    print(f"Email Generation Complete")
    print(f"  Emails generated: {len(results)}")
    print(f"  Failed: {len(leads) - len(results)}")
    print(f"  Review in sheet, then run: push_campaign.py --sheet_url '...' --campaign_name '...'")
    print(f"  Or add to existing campaign: push_campaign.py --sheet_url '...' --campaign_id '...' --campaign_name '...'")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
