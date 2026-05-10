"""
Render personalized email bodies for both campaigns from a user-provided
template file using simple {{var}} substitution.

Lenders mode:
  Reads:  col A=lender_name, col F=company_size, col K=lender_city,
          col M=lender_state, col AB=dm_name
  Filter: col C has email AND size in {1-10, 11-50, 51-200}
  Vars:   {{company}}, {{city}}, {{state}}, {{first_name}}
  Writes: col AE=campaign_body

Borrowers mode:
  Reads:  col A=borrower_name, col B=city, col C=state, col D=loan_amount,
          col G=naics_description, col R=first_name
  Filter: col Q has email
  Vars:   {{company}}, {{city}}, {{state}}, {{first_name}},
          {{loan_amount}}, {{industry}}
  Writes: col T=campaign_body

Run:
  python3 -W ignore render_bodies.py --campaign lenders \
      --template_file templates/lenders.txt
  python3 -W ignore render_bodies.py --campaign borrowers \
      --template_file templates/borrowers.txt [--overwrite]
"""

import os
import re
import sys
import json
import time
import argparse
from openai import AzureOpenAI
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

LENDER_SHEET_ID = "1-FxOuYFeI7xu76tcwkFAIcDVJuLSZg-5lBWcrqh7pIo"
LENDER_TAB = "dataset_usda-lenders_2026-04-15_15-07-12-086"
BORROWER_SHEET_ID = "1WgIhmQmJ1XhYHIVb6DgPuvBG1ex1_k76fPvr9BBVfR0"
BORROWER_TAB = "dataset_sba-rural-loans_2026-04-16_05-40-32-227"

BATCH = 10
SHEET_WRITE_DELAY = 1


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


def render(template, vars_dict):
    """Replace {{var}} placeholders. Missing vars become empty string."""
    def sub(match):
        key = match.group(1).strip()
        return str(vars_dict.get(key, ""))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template)


def first_name_from(full_name, fallback="there"):
    if not full_name or not full_name.strip():
        return fallback
    return full_name.strip().split()[0]


CLEAN_PROMPT = """Clean these business names for use in casual cold emails. Apply these rules carefully:

1. Remove legal suffixes (LLC, L.L.C., Inc, Inc., Corp, Corporation, Ltd, Limited, PLLC, LLP, P.C., P.A., Co.)
2. Convert ALL CAPS to Title Case (e.g. RESOLUTE BANK -> Resolute Bank)
3. KEEP core brand identity — do NOT drop descriptors like Bank, Capital, Hauling, Construction, Services, Group
4. Preserve acronyms in original casing (SPG stays SPG, GP stays GP)
5. Handle possessives (KATIE'S -> Katie's, GP'S -> GP's)
6. Lowercase connectors mid-name (BANK OF VERSAILLES -> Bank of Versailles)
7. If suffix is concatenated to a word (SERVICESLLC, INCLLC, etc.), strip the suffix cleanly (SERVICESLLC -> Services)
8. Strip trailing commas/periods

Examples:
- NORTH AVENUE CAPITAL, LLC -> North Avenue Capital
- BANK OF VERSAILLES -> Bank of Versailles
- WILD YOSEMITE LLC -> Wild Yosemite
- KATIE'S KITCHEN LLC -> Katie's Kitchen
- SPG Meyers LLC -> SPG Meyers
- SELF MADE WELDING SERVICESLLC -> Self Made Welding Services
- GP's Heating & Air LLC -> GP's Heating & Air
- ARTISAN CONSTRUCTION LLC -> Artisan Construction

Names to clean:
{names}

Output format: ONLY a numbered list with the cleaned name. No explanations, no quotes."""


def llm_clean_batch(client, deployment, names):
    if not names:
        return {}
    numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    try:
        resp = client.chat.completions.create(
            model=deployment,
            max_completion_tokens=4000,
            messages=[{"role": "user", "content": CLEAN_PROMPT.format(names=numbered)}],
        )
        text = (resp.choices[0].message.content or "").strip()
        cleaned = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ". " in line:
                cleaned.append(line.split(". ", 1)[1].strip().strip('"').strip("'"))
            elif ") " in line:
                cleaned.append(line.split(") ", 1)[1].strip().strip('"').strip("'"))
            else:
                cleaned.append(line.strip().strip('"').strip("'"))
        if len(cleaned) != len(names):
            print(f"  ! LLM returned {len(cleaned)} for {len(names)} inputs", flush=True)
            while len(cleaned) < len(names):
                cleaned.append(names[len(cleaned)])
        return dict(zip(names, cleaned[:len(names)]))
    except Exception as e:
        print(f"  ! LLM error: {e}", flush=True)
        return {n: n for n in names}


def llm_clean_companies(names_list, batch_size=40):
    """Clean company names in batches via Azure OpenAI.
    Returns dict: original -> cleaned."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    if not (endpoint and api_key and deployment):
        print("  ! Azure OpenAI env vars missing — using names as-is", flush=True)
        return {n: n for n in names_list}
    client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
    unique = list(dict.fromkeys(names_list))  # preserve order, dedupe
    result = {}
    for i in range(0, len(unique), batch_size):
        batch = unique[i:i+batch_size]
        bn = i // batch_size + 1
        total = (len(unique) + batch_size - 1) // batch_size
        print(f"  Cleaning batch {bn}/{total} ({len(batch)} names)...", flush=True)
        result.update(llm_clean_batch(client, deployment, batch))
    return result


ROLE_LOCAL_PARTS = {
    "info", "contact", "sales", "admin", "office", "hello", "hi",
    "support", "hr", "billing", "accounting", "invoices", "invoice",
    "mail", "general", "inquiries", "inquiry", "service", "help",
    "reception", "team", "enquiries", "noreply", "no-reply",
    "webmaster", "postmaster", "feedback", "careers", "jobs",
    "press", "media", "partners", "partnerships", "main", "mailbox",
    "cservice", "customerservice", "custserv", "membership",
    "memberservices", "loans", "loan", "lending", "credit", "banking",
    "tellers", "mortgage", "mortgagelenders", "citizens",
}


def first_name_from_email(email):
    """Extract a usable name from email local part. Returns None for role-based addresses."""
    if not email or "@" not in email:
        return None
    local = email.split("@")[0].lower().strip()
    local = re.sub(r"[\d_\-]+$", "", local).rstrip("._-")
    if local in ROLE_LOCAL_PARTS:
        return None
    # firstname.lastname or firstname_lastname → take first chunk
    if "." in local or "_" in local:
        first = re.split(r"[._]+", local)[0]
        if first.isalpha() and len(first) >= 2 and first not in ROLE_LOCAL_PARTS:
            return first.capitalize()
        return None
    if not local.isalpha():
        return None
    # 2-4 chars: short first name (Mark, Joe, Jim) — needs vowel near start
    if 2 <= len(local) <= 4:
        if local[0] in "aeiou" or local[1] in "aeiou":
            return local.capitalize()
        return None
    # 5+ chars single word: if first 2 chars are both consonants, treat as
    # initial+lastname (jrowell, pperez) → strip first letter, use the lastname
    if len(local) >= 5:
        if local[0] not in "aeiou" and local[1] not in "aeiou":
            return local[1:].capitalize()
        return local.capitalize()
    return None


def title_case_city(city):
    if not city:
        return ""
    return " ".join(w.capitalize() for w in city.split())


def humanize_loan_amount(amount):
    """50000 -> $50K, 3281000 -> $3.3M"""
    try:
        n = int(str(amount).replace(",", "").strip())
    except (ValueError, AttributeError):
        return str(amount or "")
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"${n//1000}K"
    return f"${n}"


def build_lender_targets(rows):
    """Return list of {row, vars}. Filter: email in C AND size in 1-10/11-50/51-200."""
    eligible_sizes = {"1-10", "11-50", "51-200"}
    targets = []
    for i, row in enumerate(rows):
        name = row[0] if len(row) > 0 else ""
        email = row[2] if len(row) > 2 else ""
        size = row[5] if len(row) > 5 else ""
        city = row[10] if len(row) > 10 else ""
        state = row[12] if len(row) > 12 else ""
        dm_name = row[27] if len(row) > 27 else ""
        existing_body = row[30] if len(row) > 30 else ""

        if not name.strip() or not email.strip():
            continue
        if size.strip() not in eligible_sizes:
            continue

        fname = first_name_from(dm_name, fallback="")
        if not fname:
            # Try parsing from email (use first email if multiple)
            first_email = email.strip().split(";")[0].split(",")[0].strip()
            fname = first_name_from_email(first_email) or ""
        targets.append({
            "row": i + 2,
            "existing_body": existing_body.strip(),
            "vars": {
                "company": name.strip(),  # cleaned later via LLM
                "city": title_case_city(city.strip()),
                "state": state.strip(),
                "first_name": fname or "team",
                "greeting": f"Hi {fname}" if fname else "Hi",
            },
        })
    return targets


def build_borrower_targets(rows):
    """Return list of {row, vars}. Filter: email in col Q."""
    targets = []
    for i, row in enumerate(rows):
        name = row[0] if len(row) > 0 else ""
        city = row[1] if len(row) > 1 else ""
        state = row[2] if len(row) > 2 else ""
        loan_amount = row[3] if len(row) > 3 else ""
        naics = row[6] if len(row) > 6 else ""
        email = row[16] if len(row) > 16 else ""
        first = row[17] if len(row) > 17 else ""
        existing_body = row[19] if len(row) > 19 else ""

        if not name.strip() or not email.strip():
            continue

        fname = first.strip()
        targets.append({
            "row": i + 2,
            "existing_body": existing_body.strip(),
            "vars": {
                "company": name.strip(),  # cleaned later via LLM
                "city": title_case_city(city.strip()),
                "state": state.strip(),
                "first_name": fname or "there",
                "greeting": f"Hi {fname}" if fname else "Hi",
                "loan_amount": humanize_loan_amount(loan_amount),
                "industry": naics.strip(),
            },
        })
    return targets


def flush_writes(service, sheet_id, tab, col_idx, updates):
    if not updates:
        return
    data = [
        {"range": f"'{tab}'!{col_letter(col_idx)}{u['row']}", "values": [[u["body"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"  -> Wrote {len(updates)} bodies to sheet", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


def main():
    parser = argparse.ArgumentParser(description="Render personalized email bodies")
    parser.add_argument("--campaign", choices=["lenders", "borrowers"], required=True)
    parser.add_argument("--template_file", required=True, help="Path to .txt template")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-render rows that already have a body")
    parser.add_argument("--preview", type=int, default=0,
                        help="Preview N rendered bodies without writing")
    args = parser.parse_args()

    if not os.path.exists(args.template_file):
        print(f"ERROR: template not found at {args.template_file}"); sys.exit(1)

    with open(args.template_file) as f:
        template = f.read()

    if not template.strip():
        print(f"ERROR: template file is empty"); sys.exit(1)

    if args.campaign == "lenders":
        sheet_id = LENDER_SHEET_ID
        tab = LENDER_TAB
        body_col = 30  # AE
    else:
        sheet_id = BORROWER_SHEET_ID
        tab = BORROWER_TAB
        body_col = 19  # T

    print(f"=== Render {args.campaign.title()} Bodies ===\n", flush=True)
    service = get_service()

    print(f"[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:AF"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"  {len(rows)} total rows", flush=True)

    if args.campaign == "lenders":
        targets = build_lender_targets(rows)
    else:
        targets = build_borrower_targets(rows)

    print(f"  {len(targets)} qualifying for {args.campaign} campaign", flush=True)

    # Filter out rows that already have a body unless --overwrite
    if not args.overwrite:
        targets = [t for t in targets if not t["existing_body"]]
        print(f"  {len(targets)} need rendering (skipping rows with existing body)", flush=True)

    if not targets:
        print("Nothing to render."); return

    # Clean company names via Claude Haiku
    print(f"\n[2/3] Cleaning {len(targets)} company names via LLM...", flush=True)
    raw_names = [t["vars"]["company"] for t in targets]
    name_map = llm_clean_companies(raw_names)
    for t in targets:
        t["vars"]["company"] = name_map.get(t["vars"]["company"], t["vars"]["company"])

    if args.preview:
        print(f"\n[Preview] Showing {min(args.preview, len(targets))} sample bodies:\n", flush=True)
        for t in targets[:args.preview]:
            body = render(template, t["vars"])
            print(f"--- Row {t['row']} ({t['vars']['company']}) ---")
            print(body)
            print()
        return

    print(f"\n[3/3] Rendering and writing bodies...", flush=True)
    updates = []
    for t in targets:
        body = render(template, t["vars"])
        updates.append({"row": t["row"], "body": body})
        if len(updates) >= BATCH:
            flush_writes(service, sheet_id, tab, body_col, updates)
            updates = []

    if updates:
        flush_writes(service, sheet_id, tab, body_col, updates)

    print(f"\n[3/3] Done — rendered {len(targets)} bodies", flush=True)
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit", flush=True)


if __name__ == "__main__":
    main()
