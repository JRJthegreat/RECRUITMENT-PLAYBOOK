"""
Clean company names for outreach using Azure OpenAI.
Reads col A (companyName) from the Sales Navigator agencies sheet.
Writes AI-cleaned version to col Q (clean_company).
Resume-safe: skips rows already filled in col Q.
"""

import os
import re
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

BATCH_SIZE  = 10
MAX_WORKERS = 10

COL_COMPANY_RAW   = 0   # A — original Sales Navigator company name
COL_COMPANY_CLEAN = 16  # Q — AI-cleaned name (new column)

SYSTEM_PROMPT = """You will be provided with the name of a company. Your job is to clean that name for cold outreach — it will appear in a sentence like "love that [name] keeps things personal."

Rules:
1. Remove legal suffixes: Inc, Ltd, LLC, LLP, Corp, Co, GmbH, S.A., Pte, BV, etc.
2. Remove generic trailing words: Group, Partners, Consulting, Solutions, Systems, Technologies, Tech, Recruitment, Staffing, Advisors, Agency, Services, Associates, Enterprises, International, Global, Network, Division.
3. Remove punctuation, extra spaces, special characters, or emojis.
4. Keep correct capitalization (title case).
5. Do not guess or add new words.
6. If the input is not a valid company name (e.g., "N/A"), return "Invalid".
7. If the company has an acronym as its name, keep only the acronym.

Critical rule — the "natural standalone" test:
After removing suffixes, ask: would a human naturally say this name alone in conversation?
- "hey, Nimbus" → sounds natural → keep just "Nimbus"
- "hey, Compass" → sounds natural → keep just "Compass"
- "hey, Talent" → sounds generic and confusing → keep more words
- "hey, HAS" → meaningless alone → keep more words
If what remains after stripping is a single generic word (Talent, Core, Elite, Premier, Advanced, Strategic, Innovative, Dynamic, Professional, Solutions, Search, Staffing, Healthcare, Medical, Care), keep one additional meaningful word instead of stripping too far.

Examples:
Input: Nimbus Search → Output: {"company_name": "Nimbus"}
Input: Compass Recruitment Group → Output: {"company_name": "Compass"}
Input: Blue Ocean Consulting → Output: {"company_name": "Blue Ocean"}
Input: Apple Inc. → Output: {"company_name": "Apple"}
Input: Talent Strategies LLC → Output: {"company_name": "Talent Strategies"}
Input: HAS TalentSearch LLC → Output: {"company_name": "HAS Talent"}
Input: Core Healthcare Staffing → Output: {"company_name": "Core Healthcare"}
Input: HMRC Human Resource Management Center → Output: {"company_name": "HMRC"}
Input: Bridge Placements → Output: {"company_name": "Bridge"}

Respond ONLY with JSON in this exact format: {"company_name": ""}"""


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


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_gid_from_url(url):
    m = re.search(r"gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"], s["properties"]["sheetId"]
    s = meta["sheets"][0]
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
        return data.get("company_name", "").strip().lower()
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
    ap = argparse.ArgumentParser(description="Clean agency company names → col Q")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Reprocess rows already filled in col Q")
    ap.add_argument("--lowercase_only", action="store_true",
                    help="Lowercase existing col Q values without calling the LLM")
    args = ap.parse_args()

    if not (AZURE_ENDPOINT and AZURE_API_KEY):
        print("ERROR: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set")
        return

    az_client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

    print(f"=== Clean Agency Company Names → col Q ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col Q exists (index 16, needs 17 columns)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 17:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 17 - col_count,
            }}]}
        ).execute()

    # Write header
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!Q1",
        valueInputOption="RAW",
        body={"values": [["clean_company"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:Q"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    # --lowercase_only: just lowercase existing col Q values, no LLM
    if args.lowercase_only:
        updates = []
        for i, row in enumerate(data_rows):
            val = cell(row, COL_COMPANY_CLEAN)
            if val and not val.startswith("ERROR"):
                lc = val.lower()
                if lc != val:
                    updates.append({
                        "range": f"'{tab_name}'!{col_letter(COL_COMPANY_CLEAN)}{i + 2}",
                        "values": [[lc]],
                    })
        print(f"Values to lowercase: {len(updates)}")
        for b in range(0, len(updates), 50):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates[b:b + 50]},
            ).execute()
            print(f"  Wrote {min(b + 50, len(updates))}/{len(updates)}")
            time.sleep(0.3)
        print(f"=== Done — {len(updates)} values lowercased in col Q ===")
        return

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        already = cell(row, COL_COMPANY_CLEAN)
        if already and not already.startswith("ERROR") and not args.overwrite:
            continue
        name = cell(row, COL_COMPANY_RAW)
        if not name:
            continue
        leads.append({"sheet_row": i + 2, "name": name})

    print(f"Rows to process: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:20]:
            print(f"  Row {lead['sheet_row']}: {lead['name']}")
        print("\n[DRY RUN] No API calls.")
        return

    batches = [leads[b:b + BATCH_SIZE] for b in range(0, len(leads), BATCH_SIZE)]
    total_done = 0

    for idx, batch in enumerate(batches):
        results = process_batch(az_client, batch)
        updates = []
        for lead in batch:
            cleaned = results.get(lead["sheet_row"], "")
            updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_COMPANY_CLEAN)}{lead['sheet_row']}",
                "values": [[cleaned]],
            })
            print(f"  Row {lead['sheet_row']:4d}: {lead['name'][:50]:<50} → {cleaned}")
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()
        time.sleep(0.3)
        total_done += len(batch)
        print(f"  [{idx + 1}/{len(batches)}] {total_done}/{len(leads)} done\n")

    print(f"=== Done — {total_done} names cleaned → col Q ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
