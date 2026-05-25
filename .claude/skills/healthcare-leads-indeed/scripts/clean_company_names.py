"""
Cleans company names for outreach using Azure OpenAI GPT-5.1.
Reads col C (regex-cleaned company name), writes AI-cleaned version to col W.
Processes in batches of 10, skips rows already filled in col W.
"""

import os
import re
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT   = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY    = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.1")

BATCH_SIZE = 10
MAX_WORKERS = 10
SHEET_WRITE_DELAY = 0.3

COL_COMPANY_RAW   = 2   # C — regex-cleaned name (input)
COL_COMPANY_CLEAN = 22  # W — AI-cleaned name (output)

SYSTEM_PROMPT = """You will be provided with the name of a company. Your job is to clean that name for outreach purposes.

Rules:
1. Remove legal suffixes: Inc, Ltd, LLC, LLP, Corp, Co, GmbH, S.A., Pte, BV, etc.
2. Remove descriptive suffixes: Group, Partners, Consulting, Solutions, Systems, Technologies, Tech, Recruitment, Advisors, Agency, etc.
3. Remove punctuation, extra spaces, special characters, or emojis.
4. Keep correct capitalization (title case).
5. Do not guess or add new words.
6. If the input is not a valid company name (e.g., "N/A"), return "Invalid".
7. If the company has an acronym as its name, use that acronym instead of the full name.

Examples:
Input: Synergy Search → Output: {"company_name": "Synergy"}
Input: Blue Ocean Consulting → Output: {"company_name": "Blue Ocean"}
Input: Apple Inc. → Output: {"company_name": "Apple"}
Input: HMRC Human Resource Management Center → Output: {"company_name": "HMRC"}

Since the company name will appear in a cold outreach message, it must feel natural and non-robotic.

Respond ONLY with JSON in this exact format: {"company_name": ""}"""


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


def clean_one(az_client, company_name):
    try:
        resp = az_client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": company_name},
            ],
            max_completion_tokens=50,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        return data.get("company_name", "").strip()
    except Exception as e:
        return f"ERROR: {e}"


def process_batch(az_client, batch):
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(clean_one, az_client, lead["name"]): lead for lead in batch}
        for fut in as_completed(futs):
            lead = futs[fut]
            results[lead["sheet_row"]] = fut.result()
    return results


def main():
    ap = argparse.ArgumentParser(description="AI-clean company names → col W")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not (AZURE_ENDPOINT and AZURE_API_KEY):
        print("ERROR: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set in .env")
        return

    az_client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

    print(f"=== Clean Company Names (Azure OpenAI {AZURE_DEPLOYMENT}) ===\n")
    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(service, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col W exists
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 23:  # need at least col W (index 22, 1-based 23)
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 23 - col_count
            }}]}
        ).execute()
        print(f"  Expanded sheet to col W")

    # Write header
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!W1",
        valueInputOption="RAW",
        body={"values": [["Clean Company Name"]]},
    ).execute()

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:W"
    ).execute()
    all_rows = result.get("values", [])
    data_rows = all_rows[1:]

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        already = cell(row, COL_COMPANY_CLEAN)
        if already and not already.startswith("ERROR"):
            continue
        name = cell(row, COL_COMPANY_RAW)
        if not name:
            continue
        leads.append({"sheet_row": i + 2, "name": name})

    print(f"  {len(leads)} rows to process\n")

    if args.dry_run:
        for lead in leads[:20]:
            print(f"  Row {lead['sheet_row']}: {lead['name']}")
        return

    batches = [leads[b:b + BATCH_SIZE] for b in range(0, len(leads), BATCH_SIZE)]
    total_done = 0

    for idx, batch in enumerate(batches):
        results = process_batch(az_client, batch)
        data = []
        for lead in batch:
            cleaned = results.get(lead["sheet_row"], "")
            data.append({
                "range": f"'{tab_name}'!{col_letter(COL_COMPANY_CLEAN)}{lead['sheet_row']}",
                "values": [[cleaned]],
            })
            print(f"  Row {lead['sheet_row']}: {lead['name']} → {cleaned}")
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        time.sleep(SHEET_WRITE_DELAY)
        total_done += len(batch)
        print(f"  Batch {idx + 1}/{len(batches)} done ({total_done}/{len(leads)})\n")

    print(f"=== Done — {total_done} company names cleaned → col W ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
