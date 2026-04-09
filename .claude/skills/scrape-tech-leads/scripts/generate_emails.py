"""
Phase 3a: Generate personalized outreach emails using Claude → Google Sheets

Three templates:
- Perm A: Pain point led, multiple candidates (uses proof companies)
- Perm B: Urgency led, single candidate (uses specialties)
- Contract: Speed led, immediate availability

Perm rows get randomly assigned A or B (50/50 split for A/B testing).
Contract rows always get the contract template.
"""

import os
import sys
import json
import argparse
import time
import random
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

# Proof companies the client has placed at
PROOF_COMPANIES = ["GBST", "Unit4", "Casca", "ZOPA"]

SYSTEM_PROMPT = "You are an amazing email copywriter for B2B outreach."

# --- Template A: Pain point led, multiple candidates ---
PROMPT_PERM_A = """You are writing outbound emails for a tech recruitment connector, helping a recruiter place tech talent in European companies.

Your role is to fill in the variables in this template and personalize it based on the job posting data provided.

TEMPLATE:
Noticed {{COMPANY_NAME}} has a {{ROLE_TITLE}} role posted.

I might be off base, but I'd guess this is being handled through internal recruiting and inbound applicants, which can make the hiring timeline harder to predict.

I work with a tech recruiter who's placed multiple {{ROLE_TITLE_PLURAL}} in companies across Europe, such as {{PROOF_COMPANY_1}} and {{PROOF_COMPANY_2}}. He has a few pre vetted {{ROLE_TITLE_PLURAL}} with {{SPECIALTY_1}} and {{SPECIALTY_2}} experience ready to go.

RULES:
1. Tone: casual bar conversation, very spartan. No fancy language.
2. CLEAN and SHORTEN the role title:
   a. If the title is in a non-English language (German, French, Finnish, Dutch, etc.), TRANSLATE it to English first.
   b. Strip gender tags: (m/w/d), (f/m/d), (H/F), (F/M/D), (m/f/x), (H/F/X), (w/m/d), etc.
   c. Strip location/salary suffixes: "– Munich", "| Schweiz", "(£60k + benefits)", "/ Freelance", "– Alternance"
   d. Strip brackets noise: "[KHOME]", emojis, numbering prefixes, team names
   e. Shorten to how a recruiter would actually say it in conversation. Examples:
   - "Senior Natural Language Processing Engineer" → "Senior NLP Engineer"
   - "Machine Learning Engineer - Computer Vision" → "ML Engineer"
   - "Lead Data Engineer (Cloud Platform)" → "Lead Data Engineer"
   - "Solutions Architect - Enterprise" → "Solutions Architect"
   - "Blockchain Smart Contract Developer" → "Smart Contract Developer"
   - "Ingénieur Data Scientist" → "Data Scientist"
   - "Referent der Geschäftsführung CTO – IT Strategie (m/w/d)" → "CTO"
   - "Ingénieur SRE / DevOps PostgreSQL (H/F)" → "SRE / DevOps Engineer"
3. ROLE_TITLE_PLURAL: Pluralize the shortened role title (e.g., "NLP Engineers", "ML Engineers").
4. PROOF_COMPANY_1 and PROOF_COMPANY_2: Pick the 2 most relevant companies from this list based on the target company's industry: {proof_companies}. If none are a great fit, pick the 2 that are closest.
5. SPECIALTY_1 and SPECIALTY_2: These are the MOST IMPORTANT part of the email. Read the job description carefully and find the 2 things that make this hire HARD TO FILL. What specific, niche requirement would make the hiring manager think "we've been struggling to find someone with exactly this"?
   - Look for: specific frameworks/tools (PyTorch, Kubernetes, Terraform), niche domains (real-time NLP pipelines, smart contract auditing, MLOps at scale), rare combinations (Solidity + DeFi protocol design), scale challenges (processing 10M+ events/day), certifications, or uncommon skill combos.
   - BAD examples (too generic): "Python", "team management", "cloud computing", "agile", "AI", "data experience", "Java skills", "fintech experience"
   - GOOD examples (specific pain points): "real-time NLP pipelines at scale", "Kubernetes multi-cluster orchestration", "Solidity smart contract auditing", "MLOps pipeline automation with Kubeflow", "data lake architecture on Databricks", "Kafka event streaming at high throughput", "computer vision with YOLO"
   - If the job description is vague, infer from the company context what would be hard to find.
7. CLEAN the company name. Strip legal suffixes, geographic tags, and generic words to get the casual version. Examples:
   - "TechCorp International Ltd" → "TechCorp"
   - "Alpine Solutions Group" → "Alpine"
   - "DataWorks Technologies GmbH" → "DataWorks"
   Remove: Inc, LLC, Corp, Ltd, Co., GmbH, AG, SA, BV, Group, Services, Solutions, Holdings, International, Technologies, "The" prefix.
   IMPORTANT: If the company name is in ALL CAPS, convert it to proper Title Case (e.g., "THALES GROUP" → "Thales", "SCOTTISH POWER" → "Scottish Power"). Short acronyms (2-4 chars like KLA, N26) can stay uppercase.
8. No exclamation points. No em dashes. Use commas instead.
9. Keep proper capitalization for names and titles.
10. Respond in JSON only: {{"body": "the email body here"}}

INPUT:
Company: {company_name}
Role: {job_title}
Location: {job_location}
Industry: {company_industry}
Company Description: {company_description}
Job Description (first 1000 chars): {job_description}
"""

# --- Template B: Urgency led, single candidate ---
PROMPT_PERM_B = """You are writing outbound emails for a tech recruitment connector, helping a recruiter place tech talent in European companies.

Your role is to fill in the variables in this template and personalize it based on the job posting data provided.

TEMPLATE:
Noticed {{COMPANY_NAME}} posted a {{ROLE_TITLE}} role. Is this hire a priority in the next 14 days?

Asking because I'm connected with a tech recruiter who has a {{LOCATION}} based {{ROLE_TITLE}} who just became available. {{YEARS}}+ years as a {{ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

Open to interviewing this week if filling this role is urgent.

RULES:
1. Tone: casual bar conversation, very spartan. No fancy language.
2. CLEAN and SHORTEN the role title:
   a. If the title is in a non-English language (German, French, Finnish, Dutch, etc.), TRANSLATE it to English first.
   b. Strip gender tags: (m/w/d), (f/m/d), (H/F), (F/M/D), (m/f/x), (H/F/X), (w/m/d), etc.
   c. Strip location/salary suffixes: "– Munich", "| Schweiz", "(£60k + benefits)", "/ Freelance", "– Alternance"
   d. Strip brackets noise: "[KHOME]", emojis, numbering prefixes, team names
   e. Shorten to how a recruiter would actually say it in conversation. Examples:
   - "Senior Natural Language Processing Engineer" → "Senior NLP Engineer"
   - "Machine Learning Engineer - Computer Vision" → "ML Engineer"
   - "Lead Data Engineer (Cloud Platform)" → "Lead Data Engineer"
   - "Solutions Architect - Enterprise" → "Solutions Architect"
   - "Ingénieur Data Scientist" → "Data Scientist"
   - "DevOps Engineer (m/w/d) für die App Factory" → "DevOps Engineer"
3. LOCATION: Use the job location provided. If the job is remote or location is unclear, OMIT the location entirely — write "who has a {{ROLE_TITLE}} who just became available" instead of "who has a {{LOCATION}} based {{ROLE_TITLE}}...".
4. YEARS: Use the years of experience required in the job description + 2 years. If not stated, estimate for the role level + 2.
5. INDUSTRY: The target industry vertical (e.g., "fintech", "healthtech", "SaaS").
6. SPECIALTY_1 and SPECIALTY_2: These are the MOST IMPORTANT part of the email. Read the job description carefully and find the 2 things that make this hire HARD TO FILL. What specific, niche requirement would make the hiring manager think "we've been struggling to find someone with exactly this"?
   - Look for: specific frameworks/tools (PyTorch, Kubernetes, Terraform), niche domains (real-time NLP pipelines, smart contract auditing, MLOps at scale), rare combinations (Solidity + DeFi protocol design), scale challenges (processing 10M+ events/day), certifications, or uncommon skill combos.
   - BAD examples (too generic): "Python", "team management", "cloud computing", "agile"
   - GOOD examples (specific pain points): "real-time NLP pipelines at scale", "Kubernetes multi-cluster orchestration", "Solidity smart contract auditing", "MLOps pipeline automation with Kubeflow", "data lake architecture on Databricks"
   - If the job description is vague, infer from the company context what would be hard to find.
7. CLEAN the company name. Strip legal suffixes, geographic tags, and generic words. Remove: Inc, LLC, Corp, Ltd, Co., GmbH, AG, SA, BV, Group, Services, Solutions, Holdings, International, Technologies, "The" prefix.
8. No exclamation points. No em dashes. Use commas instead.
9. Keep proper capitalization for names and titles.
10. Use correct grammar: "an" before vowel sounds (an Engineering Manager, an AI Engineer), "a" before consonant sounds (a Senior Engineer, a CTO).
11. Respond in JSON only: {{"body": "the email body here", "cleaned_role": "the cleaned and shortened role title"}}. The cleaned_role should be the final role title you used in the email body.

INPUT:
Company: {company_name}
Role: {job_title}
Location: {job_location}
Industry: {company_industry}
Company Description: {company_description}
Job Description (first 1000 chars): {job_description}
"""

# --- Contract Template: Speed led, immediate availability ---
PROMPT_CONTRACT = """You are writing outbound emails for a tech recruitment connector, helping a recruiter place tech contractors in European companies.

Your role is to fill in the variables in this template and personalize it based on the job posting data provided.

TEMPLATE:
Noticed {{COMPANY_NAME}} is looking for a {{ROLE_TITLE}} on a contract basis. How soon do you need someone to start?

Asking because I'm connected with a tech recruiter who has a {{ROLE_TITLE}} available to start immediately. {{YEARS}}+ years as a {{ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

His agency also handles the contracting logistics so your team doesn't have to.

RULES:
1. Tone: casual bar conversation, very spartan. No fancy language.
2. CLEAN and SHORTEN the role title:
   a. If the title is in a non-English language (German, French, Finnish, Dutch, etc.), TRANSLATE it to English first.
   b. Strip gender tags: (m/w/d), (f/m/d), (H/F), (F/M/D), (m/f/x), (H/F/X), (w/m/d), etc.
   c. Strip location/salary suffixes, brackets noise, emojis, numbering prefixes, team names.
   d. Shorten to how a recruiter would actually say it in conversation. Examples:
   - "Senior Natural Language Processing Engineer" → "Senior NLP Engineer"
   - "Machine Learning Engineer - Computer Vision" → "ML Engineer"
   - "Lead Data Engineer (Cloud Platform)" → "Lead Data Engineer"
3. YEARS: Use the years of experience required in the job description + 2 years. If not stated, estimate for the role level + 2.
4. INDUSTRY: The target industry vertical (e.g., "fintech", "healthtech", "SaaS").
5. SPECIALTY_1 and SPECIALTY_2: Same rules as always — the 2 things making this hire HARD TO FILL. Specific, niche requirements from the job description, not generic skills.
   - BAD: "Python", "cloud computing", "agile"
   - GOOD: "real-time NLP pipelines at scale", "Kubernetes multi-cluster orchestration", "Solidity smart contract auditing"
6. CLEAN the company name. Strip legal suffixes, geographic tags, and generic words. Remove: Inc, LLC, Corp, Ltd, Co., GmbH, AG, SA, BV, Group, Services, Solutions, Holdings, International, Technologies, "The" prefix.
7. No exclamation points. No em dashes. Use commas instead.
8. Keep proper capitalization for names and titles.
9. Keep "on a contract basis" and "His agency also handles the contracting logistics so your team doesn't have to" lines exactly as written.
10. Respond in JSON only: {{"body": "the email body here"}}

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


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def split_name(full_name):
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def clean_company_case(name):
    """Fix ALL CAPS company names to Title Case. Leave mixed-case names alone."""
    if not name:
        return name
    # Skip short acronyms (2-4 chars all caps) — they're intentional (KLA, N26, ZOPA)
    if len(name) <= 4 and name.isupper():
        return name
    # If the name is ALL CAPS (5+ chars), title-case it
    if name.isupper():
        return name.title()
    # Check individual words — fix words that are 5+ chars and all caps
    words = name.split()
    cleaned = []
    for w in words:
        if w.isupper() and len(w) >= 5:
            cleaned.append(w.title())
        else:
            cleaned.append(w)
    return " ".join(cleaned)


def generate_email(client, lead, retry=0):
    """Generate email copy for a single lead using Claude."""
    template = lead["template"]
    company_name = clean_company_case(lead["company_name"])

    if template == "perm_a":
        prompt = PROMPT_PERM_A.format(
            proof_companies=", ".join(PROOF_COMPANIES),
            company_name=company_name,
            job_title=lead["job_title"],
            job_location=lead["job_location"],
            company_industry=lead["company_industry"],
            company_description=lead["company_description"][:300],
            job_description=lead["job_description"][:1000],
        )
    elif template == "perm_b":
        prompt = PROMPT_PERM_B.format(
            company_name=company_name,
            job_title=lead["job_title"],
            job_location=lead["job_location"],
            company_industry=lead["company_industry"],
            company_description=lead["company_description"][:300],
            job_description=lead["job_description"][:1000],
        )
    else:  # contract
        prompt = PROMPT_CONTRACT.format(
            company_name=company_name,
            job_title=lead["job_title"],
            job_location=lead["job_location"],
            company_industry=lead["company_industry"],
            company_description=lead["company_description"][:300],
            job_description=lead["job_description"][:1000],
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
        cleaned_role = data.get("cleaned_role", "")

        # Clean em dashes → commas
        body = body.replace("\u2014", ",").replace("\u2013", ",")

        # Ensure "His agency" starts on a new line for contract template
        if "His agency" in body and "\n\nHis agency" not in body:
            body = body.replace("His agency", "\n\nHis agency")

        return body, cleaned_role

    except anthropic.RateLimitError:
        if retry < MAX_RETRIES:
            wait = (2 ** retry) * 2
            time.sleep(wait)
            return generate_email(client, lead, retry + 1)
        return None, None
    except (json.JSONDecodeError, Exception) as e:
        if retry < MAX_RETRIES:
            time.sleep(1)
            return generate_email(client, lead, retry + 1)
        print(f"  Error for {lead['company_name']}: {e}")
        return None, None


def read_tab_data(service, sheet_id, tab_name):
    """Read all rows from a tab. Returns (headers, all_rows)."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return [], []
    return all_rows[0], all_rows


def get_sheet_gid(service, sheet_id, tab_name):
    """Get the sheetId (gid) for a tab by name."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab_name:
            return sheet["properties"]["sheetId"]
    return None


def ensure_template_variant_header(service, sheet_id, tab_name, headers):
    """Add template_variant header if it doesn't exist. Returns its column index."""
    if "template_variant" in headers:
        return headers.index("template_variant")

    new_col_idx = len(headers)

    # Expand the grid if needed
    gid = get_sheet_gid(service, sheet_id, tab_name)
    if gid is not None:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{
                "appendDimension": {
                    "sheetId": gid,
                    "dimension": "COLUMNS",
                    "length": 1,
                }
            }]},
        ).execute()

    col = col_letter(new_col_idx)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col}1",
        valueInputOption="RAW",
        body={"values": [["template_variant"]]},
    ).execute()
    return new_col_idx


def main():
    parser = argparse.ArgumentParser(description="Generate outreach emails using Claude (3 templates)")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--tab", default="Data",
                        help="Tab to process (default: Data), or comma-separated names")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing email bodies")
    parser.add_argument("--preview", type=int, default=0, help="Preview N emails without writing to sheet")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process (0 = all)")
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

    # Determine which tabs to process
    tabs_to_process = [t.strip() for t in args.tab.split(",") if t.strip()]

    # Collect leads from all tabs
    all_leads = []
    tab_col_info = {}  # tab_name → {indices, variant_col}

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
            print(f"  Missing columns: {', '.join(missing)}")
            continue

        # Ensure template_variant and cleaned_role columns exist
        idx_variant = ensure_template_variant_header(service, sheet_id, tab_name, headers)
        # Add cleaned_role column right after template_variant
        idx_cleaned_role = col_idx("cleaned_role")
        if idx_cleaned_role is None:
            idx_cleaned_role = idx_variant + 1
            gid = get_sheet_gid(service, sheet_id, tab_name)
            if gid is not None:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"appendDimension": {"sheetId": gid, "dimension": "COLUMNS", "length": 1}}]},
                ).execute()
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{tab_name}'!{col_letter(idx_cleaned_role)}1",
                valueInputOption="RAW",
                body={"values": [["cleaned_role"]]},
            ).execute()
            print(f"  Added cleaned_role column at {col_letter(idx_cleaned_role)}")

        tab_col_info[tab_name] = {
            "body": idx_body,
            "firstname": idx_firstname,
            "lastname": idx_lastname,
            "variant": idx_variant,
            "cleaned_role": idx_cleaned_role,
        }

        tab_count = 0
        for i, row in enumerate(all_rows[1:], start=2):
            # Respect --limit
            if args.limit and len(all_leads) >= args.limit:
                break

            def cell(idx):
                if idx is None:
                    return ""
                return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

            person_name = cell(idx_person)
            email = cell(idx_email)
            body = cell(idx_body)

            # Must have DM and a real email (skip not_found markers)
            if not email or email == "not_found" or not person_name:
                continue

            # Skip if body already exists (unless --overwrite)
            if body and not args.overwrite:
                continue

            first_name, last_name = split_name(person_name)

            template = "perm_b"
            greeting = f"Hi {first_name}\n\n"

            all_leads.append({
                "tab": tab_name,
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
                "template": template,
                "greeting": greeting,
            })
            tab_count += 1

        perm_a = sum(1 for l in all_leads if l["tab"] == tab_name and l["template"] == "perm_a")
        perm_b = sum(1 for l in all_leads if l["tab"] == tab_name and l["template"] == "perm_b")
        contract = sum(1 for l in all_leads if l["tab"] == tab_name and l["template"] == "contract")
        print(f"  {tab_count} leads to generate (A: {perm_a}, B: {perm_b}, Contract: {contract})")

    if not all_leads:
        print("\nNo leads need email generation")
        sys.exit(0)

    BATCH_SIZE = 10
    total_batches = (len(all_leads) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nGenerating emails for {len(all_leads)} leads in batches of {BATCH_SIZE} ({total_batches} batches)...\n")

    client = anthropic.Anthropic(api_key=api_key)
    results = []
    failed = 0

    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch = all_leads[batch_start:batch_start + BATCH_SIZE]

        print(f"  Batch {batch_num + 1}/{total_batches}")

        # Preview mode: stop after enough
        if args.preview > 0 and len(results) >= args.preview:
            break

        batch_results = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_lead = {
                executor.submit(generate_email, client, lead): lead
                for lead in batch
            }
            for future in as_completed(future_to_lead):
                lead = future_to_lead[future]
                body, cleaned_role = future.result()
                if body:
                    full_body = f"{lead['greeting']}{body}"
                    r = {
                        "tab": lead["tab"],
                        "row_num": lead["row_num"],
                        "first_name": lead["first_name"],
                        "last_name": lead["last_name"],
                        "body": full_body,
                        "template": lead["template"],
                        "cleaned_role": cleaned_role or "",
                    }
                    batch_results.append(r)
                    results.append(r)
                    print(f"    [{lead['tab']}] Row {lead['row_num']}: {lead['company_name']} — {lead['template']}")
                else:
                    print(f"    [{lead['tab']}] Row {lead['row_num']}: {lead['company_name']} — FAILED")
                    failed += 1

        # Write batch to sheet immediately (unless preview mode)
        if batch_results and args.preview == 0:
            # Group updates by tab
            for tab_name, info in tab_col_info.items():
                tab_results = [r for r in batch_results if r["tab"] == tab_name]
                if not tab_results:
                    continue

                updates = []
                body_col = col_letter(info["body"])
                variant_col = col_letter(info["variant"])
                role_col = col_letter(info["cleaned_role"])

                for r in tab_results:
                    updates.append({
                        "range": f"'{tab_name}'!{body_col}{r['row_num']}",
                        "values": [[r["body"]]],
                    })
                    updates.append({
                        "range": f"'{tab_name}'!{variant_col}{r['row_num']}",
                        "values": [[r["template"]]],
                    })
                    updates.append({
                        "range": f"'{tab_name}'!{role_col}{r['row_num']}",
                        "values": [[r["cleaned_role"]]],
                    })
                    if info["firstname"] is not None:
                        updates.append({
                            "range": f"'{tab_name}'!{col_letter(info['firstname'])}{r['row_num']}",
                            "values": [[r["first_name"]]],
                        })
                    if info["lastname"] is not None:
                        updates.append({
                            "range": f"'{tab_name}'!{col_letter(info['lastname'])}{r['row_num']}",
                            "values": [[r["last_name"]]],
                        })

                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()

            print(f"    → Written to sheet. Running total: {len(results)} done, {failed} failed\n")

    if not results:
        print("No emails generated")
        sys.exit(1)

    # Preview mode
    if args.preview > 0:
        print(f"\n{'='*60}")
        print(f"PREVIEW (first {min(args.preview, len(results))} emails):\n")
        for r in results[:args.preview]:
            print(f"[{r['template']}] Row {r['row_num']}:")
            print(f"  {r['body']}\n")
            print(f"  Best,\n  Jude\n")
            print(f"{'='*60}")
        if args.preview < len(results):
            print(f"... and {len(results) - args.preview} more")
        print("\nRun without --preview to write to sheet.")
        sys.exit(0)

    # Summary
    perm_a_count = sum(1 for r in results if r["template"] == "perm_a")
    perm_b_count = sum(1 for r in results if r["template"] == "perm_b")
    contract_count = sum(1 for r in results if r["template"] == "contract")

    print(f"\n{'='*50}")
    print(f"Email Generation Complete")
    print(f"  Total generated: {len(results)}")
    print(f"  Perm A (pain point): {perm_a_count}")
    print(f"  Perm B (urgency): {perm_b_count}")
    print(f"  Contract: {contract_count}")
    print(f"  Failed: {failed}")
    print(f"  Review in sheet, then run push_campaign.py to send to Instantly")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
