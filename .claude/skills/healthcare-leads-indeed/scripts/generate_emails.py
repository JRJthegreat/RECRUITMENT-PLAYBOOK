"""
Generate outreach email bodies for healthcare staffing agency leads.

Logic:
- Check col O (company_about) + col T (Reasoning) for physician placement keywords
- If physicians mentioned → role_type = "doctors, NPs and PAs"
- Otherwise (no description or no physician mention) → role_type = "NPs and PAs"
- company_type: random 50/50 "small medical practices" / "private medical practices"
- Writes body to col X. Skips rows already filled.
"""

import os
import re
import json
import time
import random
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

BATCH_SIZE = 10
SHEET_WRITE_DELAY = 0.3

COL_COMPANY_ABOUT = 14  # O
COL_REASONING     = 19  # T
COL_FIRST_NAME    = 20  # U
COL_CLEAN_COMPANY = 22  # W
COL_BODY          = 23  # X

PHYSICIAN_PATTERN = re.compile(
    r"\b(physician|doctor|doctors|MD|D\.O\.|DO\b|family medicine|locum|"
    r"attending|hospitalist|medical doctor|primary care|internist|specialist)\b",
    re.IGNORECASE,
)

COMPANY_TYPES = ["small medical practices", "private medical practices"]

TEMPLATE = (
    "I'm in direct communication with a few {company_type} hiring for "
    "{role_type} right now. They struggle to compete with larger health "
    "systems and hospitals to attract quality talent, and they are open "
    "to introductions to healthcare staffing firms.\n\n"
    "Would love to send some your way."
)


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
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_gid_from_url(url):
    m = re.search(r"gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta["sheets"]
    gid = get_gid_from_url(url)
    if gid is not None:
        for s in sheets:
            if s["properties"]["sheetId"] == gid:
                return s["properties"]["title"], s["properties"]["sheetId"]
    s = sheets[0]
    return s["properties"]["title"], s["properties"]["sheetId"]


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def places_physicians(about, reasoning):
    text = about + " " + reasoning
    return bool(PHYSICIAN_PATTERN.search(text))


def build_body(about, reasoning):
    role_type = "doctors, NPs and PAs" if places_physicians(about, reasoning) else "NPs and PAs"
    company_type = random.choice(COMPANY_TYPES)
    return TEMPLATE.format(company_type=company_type, role_type=role_type), role_type


def main():
    ap = argparse.ArgumentParser(description="Generate outreach bodies for healthcare staffing leads")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    print("=== Generate Outreach Emails — Healthcare Staffing ===\n")
    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(service, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col X exists
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 24:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 24 - col_count,
            }}]}
        ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!X1",
        valueInputOption="RAW",
        body={"values": [["Body"]]},
    ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:X"
    ).execute()
    data_rows = result.get("values", [])[1:]

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        if cell(row, COL_BODY):
            continue
        first_name = cell(row, COL_FIRST_NAME)
        if not first_name:
            continue
        leads.append({
            "sheet_row": i + 2,
            "first_name": first_name,
            "about": cell(row, COL_COMPANY_ABOUT),
            "reasoning": cell(row, COL_REASONING),
            "clean_company": cell(row, COL_CLEAN_COMPANY),
        })

    doctors = sum(1 for l in leads if places_physicians(l["about"], l["reasoning"]))
    nurses  = len(leads) - doctors
    print(f"  {len(leads)} leads to process: {doctors} → doctors+NPs+PAs | {nurses} → NPs+PAs only\n")

    if args.dry_run:
        for lead in leads[:10]:
            body, role = build_body(lead["about"], lead["reasoning"])
            print(f"  Row {lead['sheet_row']} [{role}]: {lead['first_name']} @ {lead['clean_company']}")
            print(f"    {body[:120]}...\n")
        return

    batches = [leads[b:b + BATCH_SIZE] for b in range(0, len(leads), BATCH_SIZE)]
    total_done = 0

    for idx, batch in enumerate(batches):
        data = []
        for lead in batch:
            body, role = build_body(lead["about"], lead["reasoning"])
            data.append({
                "range": f"'{tab_name}'!{col_letter(COL_BODY)}{lead['sheet_row']}",
                "values": [[body]],
            })
            print(f"  Row {lead['sheet_row']} [{role}]: {lead['first_name']} @ {lead['clean_company']}")
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        time.sleep(SHEET_WRITE_DELAY)
        total_done += len(batch)

    print(f"\nDone — {total_done} email bodies written to col X")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
