"""
Phase 4: Generate personalized tech outreach emails using Claude Opus 4.5.

Reads lead data from the sheet, generates email copy per lead, and writes
First Name, Last Name, Email Body, template_variant, cleaned_role columns.
Does NOT push to Instantly — that's push_campaign.py's job.

IMPORTANT: TEMPLATE is a placeholder. Get user-approved copy and slot it in
before running without --preview. SKILL.md gates this with explicit approval.
"""

import os
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

MODEL = "claude-opus-4-5"
MAX_WORKERS = 5
MAX_RETRIES = 3
BATCH_SIZE = 10
SHEET_WRITE_DELAY = 1.5

SYSTEM_PROMPT = "You are an amazing email copywriter for B2B tech recruitment outreach across European markets."

# Column indices (pull_dataset.py HEADERS — canonical tech layout)
COL_JOB_TITLE = 1         # B
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
COL_TEMPLATE_VARIANT = 27 # AB
COL_CLEANED_ROLE = 28     # AC


# ============================================================
# TEMPLATE — placeholder. User will provide final copy.
# Available variables (rendered into the body):
#   {{COMPANY_NAME}}, {{ROLE_TITLE}}, {{LOCATION}}, {{INDUSTRY}},
#   {{SPECIALTY_1}}, {{SPECIALTY_2}}, {{YEARS}}
# Single template covers Perm, Contract, and Freelance tech roles.
# ============================================================
TEMPLATE = """<<TBD — waiting on user-approved copy>>

Noticed {{COMPANY_NAME}} posted a {{ROLE_TITLE}} role. Is this hire a priority in the next 14 days?

Asking because I'm working with a recruiter who has a {{LOCATION}} based {{ROLE_TITLE}} who just became available. {{YEARS}} as a {{ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

Open to interviewing this week if filling this role is urgent.
"""


SHARED_RULES = """
RULES:
1. Tone: casual conversation, very spartan. No fancy language. When listing alternatives use slashes not "or" — "React/TypeScript" not "React or TypeScript".
2. REWRITE the role title the way a European tech recruiter would say it out loud. Not just shortened — rewritten so it sounds natural and human.
   - "Senior Python Developer" → "Senior Python Engineer"
   - "Sr. Software Engineer II (Backend)" → "Senior Backend Engineer"
   - "Staff Backend Engineer (Go)" → "Staff Go Engineer"
   - "Lead DevOps / SRE Engineer" → "Lead SRE"
   - "Principal Full-Stack Engineer - React/Node" → "Principal Full-Stack Engineer"
   - "Machine Learning Engineer - LLMs" → "ML Engineer"
   - "Head of Engineering (Platform)" → "Head of Platform Engineering"
   - "Engineering Manager - Mobile" → "Mobile Engineering Manager"
   Key rule: the specialty leads, the level qualifies it. Always prefer "Engineer" over "Developer". Strip parentheticals, bracketed tags, team names, redundant suffixes.
3. Do not hallucinate locations. If remote or unclear, omit location entirely (drop "{{LOCATION}} based" from the sentence).
4. No exclamation points. No em dashes. Use commas instead.
5. European city handling: London/Berlin/Amsterdam/Paris/Dublin/Munich/Madrid/Barcelona/Lisbon/Stockholm/Copenhagen/Helsinki/Oslo/Zurich/Vienna stay as-is. "Greater London" → "London". "Île-de-France" / "Paris, Île-de-France" → "Paris". "Greater Manchester" → "Manchester". Strip district suffixes (e.g. "Berlin-Mitte" → "Berlin"). Keep remote work as implicit (omit location).
6. CLEAN and SHORTEN the company name to the casual version. Strip legal suffixes, country tags, generic filler.
   - "Klarna Bank AB" → "Klarna"
   - "N26 GmbH" → "N26"
   - "Delivery Hero SE" → "Delivery Hero"
   - "Wise Payments Limited" → "Wise"
   - "Mistral AI SAS" → "Mistral"
   - "Revolut Ltd" → "Revolut"
   - "Booking.com B.V." → "Booking.com"
   - "Zalando SE" → "Zalando"
   Remove: Ltd, Limited, plc, GmbH, AG, SAS, SA, SARL, B.V., N.V., S.r.l., S.p.A., A/S, AB, Oy, Oyj, SE, Group, Holdings, International, (UK), (EU), "The" prefix.
7. Keep proper capitalization for names, brands, and titles (React, TypeScript, Kubernetes, AWS, PostgreSQL, GitHub, GitLab).
8. SPECIALTY_1 and SPECIALTY_2: pick 2 things from the JD that are genuinely specific to this role. Prefer concrete tech/tools over soft skills. Examples: "React/TypeScript", "Next.js app router", "Go microservices", "Rust async runtime", "Kubernetes on AWS", "Terraform IaC", "event-driven architecture with Kafka", "PostgreSQL query optimisation", "distributed systems at scale", "ML model deployment on GCP", "GraphQL federation", "SRE incident response", "CI/CD with GitHub Actions", "platform engineering for 100+ services". Avoid generics like "team player" or "good communicator".
9. YEARS: extract the years-of-experience requirement if mentioned. NEVER write less than "3+ years" — if the JD states fewer years (e.g. "1+ year", "2 years") or doesn't state a requirement at all, default to "3+ years". Examples of valid output: "3+ years", "5+ years in production", "8 years on distributed systems".
10. ALWAYS keep paragraph structure with line breaks between paragraphs. Never collapse into one block.
11. Limit output to 75 words max.
12. Respond in JSON only: {{"body": "the email body here", "cleaned_role": "the cleaned singular role title you used"}}. Use \\n\\n for paragraph breaks in the JSON string.

INPUT:
Company: {company_name}
Role: {job_title}
Location: {job_location}
Industry: {company_industry}
Company Description: {company_description}
Job Description: {job_description}
"""

PREAMBLE = """You are writing outbound emails for a pan-European tech recruitment company that places engineers, engineering managers, and technical leaders into both permanent and contract roles across product, platform, backend, frontend, full-stack, DevOps/SRE, data, ML, and mobile teams.

Your role is to write a compelling email body to get in touch with a decision maker at a company actively hiring tech talent.

You will be provided with: company name, job title they're hiring for, job description, job location, company description, and company industry.

Fill in the variables in this template and personalize it:

"""


def col_letter(idx):
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


def generate_email(client, lead, template_text, retry=0):
    """Generate email copy for a single lead using Claude Opus 4.5."""
    prompt = PREAMBLE + template_text + SHARED_RULES.format(
        company_name=lead["company_name"],
        job_title=lead["job_title"],
        job_location=lead.get("job_location", ""),
        company_industry=lead.get("company_industry", ""),
        company_description=lead.get("company_description", "")[:300],
        job_description=lead.get("job_description", ""),
    )

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        data = json.loads(text)
        body = data.get("body", "")
        cleaned_role = data.get("cleaned_role", "")

        body = body.replace("\u2014", ",").replace("\u2013", ",")

        return {"body": body, "cleaned_role": cleaned_role}

    except anthropic.RateLimitError:
        if retry < MAX_RETRIES:
            wait = (2 ** retry) * 2
            time.sleep(wait)
            return generate_email(client, lead, template_text, retry + 1)
        return None
    except (json.JSONDecodeError, Exception) as e:
        if retry < MAX_RETRIES:
            time.sleep(1)
            return generate_email(client, lead, template_text, retry + 1)
        print(f"  Error for {lead['company_name']}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Generate tech outreach emails using Claude Opus 4.5")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing email bodies")
    parser.add_argument("--limit", type=int, default=0, help="Max leads (0 = all)")
    parser.add_argument("--preview", type=int, default=0, help="Preview N emails without writing to sheet")
    parser.add_argument("--template", help="Path to template text file (overrides TEMPLATE constant)")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        return

    template_text = TEMPLATE
    if args.template:
        with open(args.template) as f:
            template_text = f.read().strip()
        print(f"  Loaded template from: {args.template}")

    if "<<TBD" in template_text and args.preview == 0:
        print("ERROR: TEMPLATE is still a placeholder.")
        print("  Slot in the user-approved copy at the top of generate_emails.py,")
        print("  or pass --template /path/to/copy.txt. Run with --preview to test scaffolding.")
        return

    print(f"=== Generate Tech Emails ({MODEL}) ===\n")

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"  Using tab: '{tab_name}'")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AC"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("  No data rows found.")
        return

    leads = []
    for i, row in enumerate(all_rows[1:]):
        if args.limit > 0 and len(leads) >= args.limit:
            break

        dm_name = cell(row, COL_DM_NAME)
        email = cell(row, COL_EMAIL)
        email_body = cell(row, COL_EMAIL_BODY)

        if not dm_name or not email or email == "not_found":
            continue
        if email_body and not args.overwrite:
            continue

        city = cell(row, COL_CITY)
        state = cell(row, COL_STATE)
        job_location = f"{city}, {state}" if city and state else city or state or ""

        leads.append({
            "sheet_row": i + 2,
            "dm_name": dm_name,
            "email": email,
            "company_name": cell(row, COL_COMPANY_NAME),
            "job_title": cell(row, COL_JOB_TITLE),
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
        if args.preview > 0 and len(results) >= args.preview:
            break

        batch = leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        batch_results = []
        print(f"  Batch {b + 1}/{total_batches}")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(generate_email, client, lead, template_text): lead
                for lead in batch
            }
            for future in as_completed(futures):
                lead = futures[future]
                result = future.result()
                if result:
                    first_name, last_name = split_name(lead["dm_name"])
                    full_body = f"Hi {first_name},\n\n{result['body']}"
                    r = {
                        "sheet_row": lead["sheet_row"],
                        "first_name": first_name,
                        "last_name": last_name,
                        "body": full_body,
                        "cleaned_role": result["cleaned_role"],
                        "company_name": lead["company_name"],
                    }
                    batch_results.append(r)
                    results.append(r)
                    print(f"    Row {lead['sheet_row']}: {lead['company_name']} — done")
                else:
                    print(f"    Row {lead['sheet_row']}: {lead['company_name']} — FAILED")
                    failed += 1

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
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_TEMPLATE_VARIANT)}{r['sheet_row']}",
                    "values": [["all"]],
                })
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_CLEANED_ROLE)}{r['sheet_row']}",
                    "values": [[r["cleaned_role"]]],
                })

            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"    → Written {len(batch_results)} emails to sheet")

        if b + 1 < total_batches:
            time.sleep(SHEET_WRITE_DELAY)

    if args.preview > 0:
        print(f"\n{'='*50}")
        print(f"PREVIEW (first {min(args.preview, len(results))} emails):\n")
        for r in results[:args.preview]:
            print(f"Row {r['sheet_row']} — {r['company_name']} (cleaned_role: {r['cleaned_role']}):")
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
