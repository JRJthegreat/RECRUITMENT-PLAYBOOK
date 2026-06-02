"""
Generate email bodies for the Single Opening tab.

Approved template:
  Hey {firstName},

  [Icebreaker]

  Noticed you're looking for a [Role] in [City]. Is this [role] hire a priority in the next 14-30 days?

  Asking because I'm connected with some who've filled [role] roles for practices similar to yours.
  Just spoke with them on the phone before sending this, and they mentioned having a few [role]s
  with 5-7+ years of [specialty] experience looking for roles in [ST] right now.

  Happy to connect you directly with them for more details.

Dynamic parts pulled from the sheet:
  - First name     — from DM Name (col AA)
  - Icebreaker     — col AE
  - Role           — derived from job title (col A): Nurse Practitioner / PA / Physician
  - City           — col H (parsed from "City, State, United States")
  - State abbrev   — col H
  - Specialty      — 2-4 words from job description via GPT-4.1

Writes to col AG "Email Body". Resume-safe. Dedupes by company.

Usage:
  python3 -W ignore generate_single_emails.py --sheet_url "URL" [--preview 5]
"""

import os
import re
import json
import time
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

TAB         = "Single Opening"
BATCH_SIZE  = 10
LLM_WORKERS = 8

COL_JOB_TITLE = 0   # A
COL_JOB_DESC  = 1   # B
COL_LOCATION  = 7   # H
COL_COMPANY   = 12  # M
COL_DM_NAME   = 26  # AA
COL_EMAIL     = 29  # AD
COL_ICEBREAKER = 30 # AE
COL_EMAIL_BODY = 32 # AG

STATE_ABBREV = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


def safe_get(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def tab_exists(service, sheet_id, title):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def ensure_columns(service, sheet_id, title, min_cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"appendDimension": {
                        "sheetId": gid, "dimension": "COLUMNS",
                        "length": min_cols - have}}]},
                ).execute()
            return


def extract_first_name(full_name):
    if not full_name:
        return "there"
    name = re.sub(
        r"\b(Dr\.?|MD|PhD|DO|NP|PA|RN|MBA|MPH|SHRM-CP|M\.Ed|LPC|CPA|VFR|Jr\.?|Sr\.?|II|III|IV)\b",
        "", full_name, flags=re.IGNORECASE,
    )
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r",", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split()
    return parts[0] if parts else "there"


def parse_city_state(location_str):
    """Parse 'City, State, United States' → (city, state_abbrev).
    Falls back to ('', '') if unparseable."""
    if not location_str:
        return "", ""
    # "Houston, Texas, United States" → city=Houston, state=Texas
    parts = [p.strip() for p in location_str.split(",")]
    city = parts[0] if parts else ""
    state_full = ""
    for part in parts[1:]:
        part = part.strip()
        if part in STATE_ABBREV:
            state_full = part
            break
        # also handle already-abbreviated
        if part in STATE_ABBREV.values():
            return city, part
    abbrev = STATE_ABBREV.get(state_full, "")
    return city, abbrev


def get_role_label(job_title):
    """Return the short role label used in the email copy."""
    t = (job_title or "").lower()
    if "physician assistant" in t or " pa-c" in t or t.endswith(" pa"):
        return "PA"
    if any(x in t for x in ("nurse practitioner", "fnp", "aprn", "np-c", "family np")):
        return "Nurse Practitioner"
    if "physician" in t and "assistant" not in t:
        return "Physician"
    return "NP"


def get_specialty(client, job_title, job_desc):
    """Return a 2-4 word specialty phrase from the job context."""
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=20, temperature=0,
            messages=[{"role": "user", "content": (
                f"Job title: {job_title}\n"
                f"Job description (first 400 chars): {job_desc[:400]}\n\n"
                "Return ONLY a 2-4 word clinical specialty phrase (e.g. 'family medicine', "
                "'wound care', 'psychiatric mental health', 'rheumatology', 'urgent care', "
                "'pain management'). No punctuation, no explanation."
            )}],
        )
        return (resp.choices[0].message.content or "").strip().strip(".")
    except Exception:
        return "primary care"


def build_email(first_name, icebreaker, role, city, state_abbrev, specialty):
    """Assemble the approved single-opening template."""
    location_str = f"in {city}" if city else f"in {state_abbrev}" if state_abbrev else ""
    state_str = f"in {state_abbrev}" if state_abbrev else ""

    # role label variations
    role_lower = role.lower()
    role_plural = f"{role}s" if not role_lower.endswith("n") else f"{role}s"
    if role == "Nurse Practitioner":
        role_plural = "NPs"
        role_short = "NP"
    elif role == "PA":
        role_plural = "PAs"
        role_short = "PA"
    else:
        role_plural = f"{role}s"
        role_short = role

    body = (
        f"Hey {first_name},\n\n"
        f"{icebreaker}\n\n"
        f"Noticed you're looking for a {role} {location_str}. "
        f"Is this {role_short} hire a priority in the next 14-30 days?\n\n"
        f"Asking because I know someone who's filled {role_short} roles for practices "
        f"similar to yours. Just spoke with them on the phone before sending this, and they "
        f"mentioned having a few {role_plural} with 5-7+ years of {specialty} experience "
        f"looking for roles {state_str} right now."
    )
    # strip any em dashes that sneak in from GPT specialty
    body = body.replace("—", ",").replace("–", ",")
    return body


def write_batch(service, sheet_id, updates):
    if not updates:
        return
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            return
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 503) and attempt < 3:
                time.sleep(4 * (2 ** attempt))
            else:
                raise


def main():
    ap = argparse.ArgumentParser(description="Generate email bodies for Single Opening tab")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    mode = f"PREVIEW ({args.preview})" if args.preview else "LIVE"
    print(f"=== Generate Single Opening Emails ({mode}) ===\n")

    if not tab_exists(service, sheet_id, TAB):
        print(f"Tab {TAB!r} not found.")
        return

    ensure_columns(service, sheet_id, TAB, COL_EMAIL_BODY + 1)
    if not args.preview:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
                {"range": f"'{TAB}'!{col_letter(COL_EMAIL_BODY)}1", "values": [["Email Body"]]}
            ]}).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB}'!A2:AG10000"
    ).execute().get("values", [])

    companies = OrderedDict()
    for i, r in enumerate(rows):
        company = safe_get(r, COL_COMPANY)
        dm = safe_get(r, COL_DM_NAME)
        email = safe_get(r, COL_EMAIL)
        body = safe_get(r, COL_EMAIL_BODY)
        ice = safe_get(r, COL_ICEBREAKER)
        if not company or not dm or not email or body or not ice:
            continue
        if company not in companies:
            companies[company] = {"sheet_rows": [], "row": r, "company": company}
        companies[company]["sheet_rows"].append(i + 2)

    todo = list(companies.values())
    print(f"Companies to generate: {len(todo)}")

    def run(lead):
        r = lead["row"]
        first_name = extract_first_name(safe_get(r, COL_DM_NAME))
        icebreaker = safe_get(r, COL_ICEBREAKER)
        job_title = safe_get(r, COL_JOB_TITLE)
        city, state_abbrev = parse_city_state(safe_get(r, COL_LOCATION))
        role = get_role_label(job_title)
        specialty = get_specialty(client, job_title, safe_get(r, COL_JOB_DESC))
        body = build_email(first_name, icebreaker, role, city, state_abbrev, specialty)
        return lead, body

    if args.preview:
        sample = todo[:args.preview]
        print(f"\nGenerating {len(sample)} previews...\n")
        for lead in sample:
            _, body = run(lead)
            print(f"=== {lead['company']} ===")
            print(body)
            print()
        return

    pending = []
    done = written = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run, lead): lead for lead in todo}
        for fut in as_completed(futs):
            lead, body = fut.result()
            done += 1
            for rn in lead["sheet_rows"]:
                pending.append({
                    "range": f"'{TAB}'!{col_letter(COL_EMAIL_BODY)}{rn}",
                    "values": [[body]],
                })
            written += 1
            if len(pending) >= BATCH_SIZE or done == len(todo):
                write_batch(service, sheet_id, pending)
                pending = []
                print(f"  {done}/{len(todo)} done ({written} written)")
                time.sleep(0.3)

    print(f"\n=== Done === {written} email bodies written to col {col_letter(COL_EMAIL_BODY)}")


if __name__ == "__main__":
    main()
