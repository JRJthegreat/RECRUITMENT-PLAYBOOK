"""
Clean company names for outreach on the Multiple Openings / Single Opening tabs.

Reads col M (Company Name), writes cleaned version to col AF (Clean Company).
Dedupes by company — one LLM call per unique name, written to all its rows.
Resume-safe: skips rows where col AF already populated.

Usage:
  python3 -W ignore clean_company_names.py --sheet_url "URL" [--preview 10]
"""

import os
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

TABS       = ["Multiple Openings", "Single Opening"]
BATCH_SIZE = 10
LLM_WORKERS = 10

COL_COMPANY = 12  # M
COL_CLEAN   = 31  # AF

SYSTEM_PROMPT = """You clean healthcare company names for use in cold outreach emails.

Rules:
1. Remove legal suffixes: Inc, LLC, PLLC, LLP, Corp, PC, MD, S.C., Ltd, etc.
2. Remove redundant descriptors that are obvious from context: Group, Partners, Solutions, Systems, Associates, etc. — ONLY if removing them still leaves a clear, natural name.
3. Keep the core identity intact. "Savannah Vascular Institute" stays as is — "Institute" is part of the brand. "GASTRODOXS, PLLC" becomes "GASTRODOXS".
4. Keep medical/clinical words that are part of the brand: Clinic, Medical, Health, Center, Care, Wellness, Practice — these are often core to the name.
5. Remove punctuation and extra spaces. Fix capitalization to title case.
6. If the name is already clean and short, return it as is.
7. Acronyms stay as acronyms (e.g. ARC, CIT, FIX).

Examples:
  "GASTRODOXS, PLLC" → "GASTRODOXS"
  "Gonzaba Medical Group" → "Gonzaba Medical"
  "Providence Community Health Centers" → "Providence Community Health"
  "Pulmonary Medicine, Infectious Disease & Critical Care Consultants Medical Group, Inc." → "Pulmonary Medicine & Critical Care Consultants"
  "Savannah Vascular Institute" → "Savannah Vascular Institute"
  "Austin Regional Clinic: ARC" → "ARC"
  "Indiana Fertility Institute" → "Indiana Fertility Institute"
  "KIDNEY CARE CENTER OF GEORGIA, LLC" → "Kidney Care Center of Georgia"
  "ZÖe Center for Pediatrics & Adolescent Health, LLC" → "ZOe Center for Pediatrics"

Return ONLY valid JSON: {"clean_name": ""}"""


def cell(row, idx):
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


def clean_one(client, raw_name):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=60, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f'Company name: "{raw_name}"'},
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return data.get("clean_name", raw_name).strip() or raw_name
    except Exception:
        return raw_name


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
    ap = argparse.ArgumentParser(description="Clean company names for outreach")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--preview", type=int, default=0,
                    help="Dry-run: show N cleaned names without writing")
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    mode = f"PREVIEW ({args.preview})" if args.preview else "LIVE"
    print(f"=== Clean Company Names ({mode}) ===\n")

    for tab in TABS:
        if not tab_exists(service, sheet_id, tab):
            continue

        ensure_columns(service, sheet_id, tab, COL_CLEAN + 1)
        if not args.preview:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{tab}'!{col_letter(COL_CLEAN)}1",
                     "values": [["Clean Company"]]}
                ]}).execute()

        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:AF10000"
        ).execute().get("values", [])

        # dedupe by raw name
        companies = OrderedDict()
        for i, r in enumerate(rows):
            raw = cell(r, COL_COMPANY)
            existing = cell(r, COL_CLEAN)
            if not raw or existing:
                continue
            companies.setdefault(raw, []).append(i + 2)

        print(f"{tab}: {len(companies)} unique names to clean")

        if args.preview:
            sample = list(companies.keys())[:args.preview]
            print()
            for name in sample:
                clean = clean_one(client, name)
                print(f"  {name[:50]:50s} → {clean}")
            print()
            continue

        pending = []
        done = written = 0

        def run(name):
            return name, clean_one(client, name)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run, n): n for n in companies}
            for fut in as_completed(futs):
                name, clean = fut.result()
                done += 1
                for rn in companies[name]:
                    pending.append({
                        "range": f"'{tab}'!{col_letter(COL_CLEAN)}{rn}",
                        "values": [[clean]],
                    })
                written += 1

                if len(pending) >= BATCH_SIZE or done == len(companies):
                    write_batch(service, sheet_id, pending)
                    pending = []
                    print(f"  {done}/{len(companies)} done")
                    time.sleep(0.3)

        print(f"  {tab}: {written} names cleaned\n")

    print("=== Done ===")


if __name__ == "__main__":
    main()
