"""
Generate email bodies for the Multiple Openings tab.

Approved template:
  [Icebreaker]

  Noticed you have [count] NP and PA roles open across [State A] and [State B] right now —
  when roles like this go unfilled, it usually means your patients aren't getting the care
  they need, and revenue is walking out the door.

  I know someone who places NPs and PAs in practices like yours. Spoke with them on the
  phone before sending this. They mentioned they have a few NPs and PAs licensed to practice
  in [ST] and [ST] with [specialty] experience for whom those roles would be a perfect fit.

  Should I CC them here for more details?

Dynamic parts pulled from the sheet:
  - Icebreaker (col AE)
  - Opening count (col X)
  - States — full and abbreviated — parsed from Openings Detail (col Y)
  - Specialty — 2-4 words from job description via GPT-4.1 (col B)

Writes to col AG "Email Body". Resume-safe. Dedupes by company.

Usage:
  python3 -W ignore generate_multi_emails.py --sheet_url "URL" [--preview 5]
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

TAB          = "Multiple Openings"
BATCH_SIZE   = 10
LLM_WORKERS  = 8

COL_JOB_TITLE    = 0   # A
COL_JOB_DESC     = 1   # B
COL_COMPANY      = 12  # M
COL_OPENINGS     = 23  # X
COL_OPENINGS_DET = 24  # Y
COL_DM_NAME      = 26  # AA
COL_EMAIL        = 29  # AD
COL_ICEBREAKER   = 30  # AE
COL_EMAIL_BODY   = 32  # AG

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


def parse_states(openings_detail):
    """Extract unique ordered states from Openings Detail string.
    Format: 'Title — City, State, United States — Date\n...'
    Returns list of unique state names in order of first appearance."""
    states = []
    seen = set()
    for line in openings_detail.split("\n"):
        # match ", State, United States" pattern
        m = re.search(r",\s*([A-Za-z ]+),\s*United States", line)
        if m:
            state = m.group(1).strip()
            if state and state not in seen and state in STATE_ABBREV:
                seen.add(state)
                states.append(state)
    return states


def join_full(states):
    """['Texas', 'California'] → 'Texas and California'"""
    if not states:
        return ""
    if len(states) == 1:
        return states[0]
    if len(states) == 2:
        return f"{states[0]} and {states[1]}"
    return ", ".join(states[:-1]) + f", and {states[-1]}"


def join_abbrev(states):
    """['Texas', 'California'] → 'TX and CA'"""
    abbrevs = [STATE_ABBREV.get(s, s) for s in states]
    if not abbrevs:
        return ""
    if len(abbrevs) == 1:
        return abbrevs[0]
    if len(abbrevs) == 2:
        return f"{abbrevs[0]} and {abbrevs[1]}"
    return ", ".join(abbrevs[:-1]) + f", and {abbrevs[-1]}"


def location_prefix(states):
    """'across' for multiple states, 'in' for one."""
    return "in" if len(states) <= 1 else "across"


def get_specialty(client, job_title, job_desc):
    """Return a 2-4 word specialty phrase from the job context."""
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=20, temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    f"Job title: {job_title}\n"
                    f"Job description (first 400 chars): {job_desc[:400]}\n\n"
                    "Return ONLY a 2-4 word clinical specialty phrase describing the experience "
                    "required (e.g. 'wound care', 'family medicine', 'psychiatric mental health', "
                    "'rheumatology', 'urgent care'). No punctuation, no explanation."
                ),
            }],
        )
        return (resp.choices[0].message.content or "").strip().strip(".")
    except Exception:
        return "primary care"


def build_email(icebreaker, count, states, specialty, job_title):
    """Assemble the approved email template."""
    prefix = location_prefix(states)
    full = join_full(states)
    abbrev = join_abbrev(states)

    # number word for small counts, digit for large
    NUM_WORDS = {1:"one",2:"two",3:"three",4:"four",5:"five",6:"six",
                 7:"seven",8:"eight",9:"nine",10:"ten",11:"eleven",12:"twelve"}
    try:
        count_int = int(count)
        count_str = NUM_WORDS.get(count_int, str(count_int))
    except (ValueError, TypeError):
        count_str = str(count)

    # role label — if only NPs in the data, say NP roles; otherwise NP and PA
    title_lower = job_title.lower()
    if "physician assistant" in title_lower or " pa " in title_lower or title_lower.endswith(" pa"):
        role_label = "NP and PA roles"
    else:
        role_label = "NP roles"

    # if no states parsed, omit the location phrase entirely
    if states:
        line1 = (f"Noticed you have {count_str} {role_label} open {prefix} {full} right now. "
                 f"When roles like this go unfilled, it usually means your patients aren't getting "
                 f"the care they need, and revenue is walking out the door.")
        line3 = (f"I know someone who places NPs and PAs in practices like yours. Spoke with them "
                 f"on the phone before sending this. They mentioned they have a few NPs and PAs "
                 f"licensed to practice in {abbrev} with {specialty} experience for whom those "
                 f"roles would be a perfect fit.")
    else:
        line1 = (f"Noticed you have {count_str} {role_label} open right now. "
                 f"When roles like this go unfilled, it usually means your patients aren't getting "
                 f"the care they need, and revenue is walking out the door.")
        line3 = (f"I know someone who places NPs and PAs in practices like yours. Spoke with them "
                 f"on the phone before sending this. They mentioned they have a few NPs and PAs "
                 f"with {specialty} experience for whom those roles would be a perfect fit.")

    body = f"{icebreaker}\n\n{line1}\n\n{line3}\n\nShould I CC them here for more details?"
    return body


def extract_first_name(full_name):
    """Pull first name from full DM name, stripping titles like Dr., MD, etc."""
    if not full_name:
        return "there"
    name = re.sub(r"\b(Dr\.?|MD|PhD|DO|NP|PA|RN|MBA|MPH|SHRM-CP|M\.Ed|LPC|CPA|VFR)\b",
                  "", full_name, flags=re.IGNORECASE).strip()
    parts = name.split()
    return parts[0] if parts else "there"


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
    ap = argparse.ArgumentParser(description="Generate email bodies for Multiple Openings tab")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--preview", type=int, default=0,
                    help="Dry-run: print N emails without writing")
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    mode = f"PREVIEW ({args.preview})" if args.preview else "LIVE"
    print(f"=== Generate Multi-Opening Emails ({mode}) ===\n")

    if not tab_exists(service, sheet_id, TAB):
        print(f"Tab {TAB!r} not found.")
        return

    ensure_columns(service, sheet_id, TAB, COL_EMAIL_BODY + 1)
    if not args.preview:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
                {"range": f"'{TAB}'!{col_letter(COL_EMAIL_BODY)}1",
                 "values": [["Email Body"]]}
            ]}).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB}'!A2:AG10000"
    ).execute().get("values", [])

    # dedupe by company — use first row's data for template generation
    companies = OrderedDict()
    for i, r in enumerate(rows):
        company = safe_get(r, COL_COMPANY)
        dm = safe_get(r, COL_DM_NAME)
        email = safe_get(r, COL_EMAIL)
        body = safe_get(r, COL_EMAIL_BODY)
        if not company or not dm or not email or body:
            continue
        if company not in companies:
            companies[company] = {"sheet_rows": [], "row": r, "company": company}
        companies[company]["sheet_rows"].append(i + 2)

    todo = list(companies.values())
    print(f"Companies to generate: {len(todo)}")

    if args.preview:
        sample = todo[:args.preview]
        for lead in sample:
            r = lead["row"]
            icebreaker = safe_get(r, COL_ICEBREAKER)
            count = safe_get(r, COL_OPENINGS)
            detail = safe_get(r, COL_OPENINGS_DET)
            states = parse_states(detail)
            specialty = get_specialty(client, safe_get(r, COL_JOB_TITLE), safe_get(r, COL_JOB_DESC))
            first_name = extract_first_name(safe_get(r, COL_DM_NAME))
            email_body = build_email(icebreaker, count, states, specialty, safe_get(r, COL_JOB_TITLE))
            email_body = f"Hey {first_name},\n\n{email_body}"
            email_body = email_body.replace("—", ",").replace("–", ",")
            print(f"=== {lead['company']} ===")
            print(email_body)
            print()
        return

    pending = []
    done = written = 0

    def run(lead):
        r = lead["row"]
        icebreaker = safe_get(r, COL_ICEBREAKER)
        count = safe_get(r, COL_OPENINGS)
        detail = safe_get(r, COL_OPENINGS_DET)
        states = parse_states(detail)
        specialty = get_specialty(client, safe_get(r, COL_JOB_TITLE), safe_get(r, COL_JOB_DESC))
        first_name = extract_first_name(safe_get(r, COL_DM_NAME))
        body = build_email(icebreaker, count, states, specialty, safe_get(r, COL_JOB_TITLE))
        # prepend greeting
        body = f"Hey {first_name},\n\n{body}"
        body = body.replace("—", ",").replace("–", ",")
        return lead, body

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
