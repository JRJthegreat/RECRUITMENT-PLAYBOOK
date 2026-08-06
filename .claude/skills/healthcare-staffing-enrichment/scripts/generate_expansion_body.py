"""
Generate the email body (col V) for the AUG 2026 expansion-angle demand
campaign — the May 25th mirror with Jude's approved edits (2026-08-04).

Clone of generate_demand_body.py per repo convention (per-campaign copy
scripts are cloned, never parameterized). Do NOT edit generate_demand_body.py
— it belongs to the completed Demand (1-50) campaign.

Template is Jude's VERBATIM-approved copy. Only {first} and {company}
(casualized, lowercase — May 25th style) are substituted. Body is plain
text with newlines; it rides as {{personalization}} in a text-only campaign.

Reads:  col A (company), col S (email_status), col T (first_name)
Writes: col V (email_body)

Only processes rows where email_status == "found" and first_name is set.
Resume-safe: skips rows where col V already populated.

Run:
  python3 -W ignore generate_expansion_body.py --sheet_url "URL" --tab "1-50 EMP" [--preview N]
"""

import os
import re
import json
import time
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

COL_COMPANY      = 0   # A
COL_EMAIL_STATUS = 18  # S
COL_FIRST_NAME   = 19  # T
COL_EMAIL_BODY   = 21  # V

WRITE_BATCH = 10

# Jude's approved template (2026-08-04). VERBATIM — only {first} and
# {company} are substituted. Do not reword without his sign-off.
BODY_LINES = [
    "Hi {first},",
    "",
    "love that {company} still keeps the human side front and center when "
    "sourcing candidates, not just letting AI do all the work. Seems like "
    "you care more about the fit, not just the fill.",
    "",
    "I stumbled across your work helping medical practices fill clinical "
    "roles. Impressive stuff.",
    "",
    "I'm connected with a few newly opened clinics and health systems "
    "opening new locations. They're building out their teams right now "
    "and are open to working with external recruiters.",
    "",
    "Could intro you if you're looking for fresh reqs.",
    "",
    "Worth a quick 15 min chat?",
]
# No sign-off — Jude 2026-08-05, standing rule: never add "Best, Jude" to
# messaging; the account signature carries identity.

# May 25th company style: lowercase, legal suffixes and generic staffing
# tails stripped ("SourcePro Search, Inc." -> "sourcepro").
LEGAL_SUFFIXES = r"(?:llc|l\.l\.c\.|inc|inc\.|corp|corp\.|corporation|ltd|ltd\.|co|co\.|pllc|lp|llp|pc|p\.c\.)"
GENERIC_TAILS = {
    "staffing", "search", "solutions", "recruiting", "recruitment",
    "group", "partners", "associates", "agency", "services", "consultants",
    "consulting", "professionals", "personnel", "talent", "firm",
    "technologies",
}
NICKNAMES = {
    "william": "Will", "robert": "Rob", "richard": "Rich", "michael": "Mike",
    "christopher": "Chris", "matthew": "Matt", "daniel": "Dan", "james": "Jim",
    "joseph": "Joe", "thomas": "Tom", "charles": "Charlie", "anthony": "Tony",
    "steven": "Steve", "stephen": "Steve", "andrew": "Andy", "kenneth": "Ken",
    "joshua": "Josh", "timothy": "Tim", "jeffrey": "Jeff", "gregory": "Greg",
    "benjamin": "Ben", "samuel": "Sam", "patricia": "Pat", "jennifer": "Jen",
    "elizabeth": "Liz", "katherine": "Kate", "kathleen": "Kathy",
    "margaret": "Maggie", "victoria": "Vicky", "alexandra": "Alex",
    "alexander": "Alex", "nicholas": "Nick", "jonathan": "Jon",
    "zachary": "Zach", "jacob": "Jake", "nathaniel": "Nate",
}


def casual_first(first):
    return NICKNAMES.get(first.strip().lower(), first.strip())


def casual_company(name):
    n = name.strip()
    n = re.sub(r"[|/].*$", "", n)                     # drop taglines after | or /
    n = re.sub(r"\s+-\s+.*$", "", n)                  # drop " - Jackson" location tails
    n = re.sub(rf"[,.]?\s*{LEGAL_SUFFIXES}\s*$", "", n, flags=re.I).strip(" ,.-")
    words = n.split()
    while len(words) > 1 and words[-1].lower().strip(",.") in GENERIC_TAILS:
        words.pop()
    return " ".join(words).lower()


def build_body(first, company):
    return "\n".join(
        line.format(first=casual_first(first), company=casual_company(company))
        for line in BODY_LINES)


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(COL_EMAIL_BODY)}{u['row']}",
             "values": [[u["body"]]]} for u in updates]
    for attempt in range(6):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
            ).execute()
            break
        except Exception as e:
            if "429" in str(e) or "RATE_LIMIT" in str(e):
                wait = min(65, 2 ** attempt * 5)
                print(f"  429 rate-limited — backing off {wait}s", flush=True)
                time.sleep(wait)
            else:
                raise
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(1.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--preview", type=int, default=0)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:V"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        status  = row[COL_EMAIL_STATUS].strip().lower() if len(row) > COL_EMAIL_STATUS else ""
        first   = row[COL_FIRST_NAME].strip() if len(row) > COL_FIRST_NAME else ""
        company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        body    = row[COL_EMAIL_BODY].strip() if len(row) > COL_EMAIL_BODY else ""
        if status != "found" or not first or not company or body:
            continue
        pending.append({"row": i + 2, "body": build_body(first, company)})

    print(f"=== Expansion body gen — tab '{tab}': {len(pending)} pending ===", flush=True)

    if args.preview:
        for p in pending[:args.preview]:
            print("=" * 60)
            print(f"row {p['row']}")
            print(p["body"])
        return

    updates = []
    for p in pending:
        updates.append(p)
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab)
            updates = []
    flush(service, updates, sheet_id, tab)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
