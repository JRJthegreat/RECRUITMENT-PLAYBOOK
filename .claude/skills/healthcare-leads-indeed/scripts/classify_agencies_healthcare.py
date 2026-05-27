"""
Classify whether each agency in the Sales Navigator sheet actually operates
in healthcare staffing.

Reads:  col A (companyName), col B (description)
Writes: col U (is_healthcare) → "true" | "false" | "uncertain"

Resume-safe: skips rows where col U already populated.
10 parallel LLM workers, batch writes every 50 rows.
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

BATCH_SIZE  = 50
MAX_WORKERS = 10

COL_COMPANY      = 0   # A
COL_DESC         = 1   # B
COL_IS_HEALTHCARE = 20  # U

SYSTEM_PROMPT = """You determine whether a company operates in healthcare staffing or medical recruitment.

Rules — apply in this order:

1. Return "true" if the company name OR description contains ANY mention of: healthcare, medical, health, nursing, nurse, physician, clinical, doctor, NP, PA, CNA, allied health, therapy, behavioral health, home care, long-term care, locum, staffing for patients, or any other healthcare/medical context — even a single sentence like "we work in the medical field" is enough.

2. Return "false" if BOTH the name AND description make it obvious the company has nothing to do with healthcare — e.g., purely IT, finance, legal, construction, or general commercial staffing with zero health/medical mention anywhere.

3. Return "uncertain" ONLY when the description is completely blank AND the company name has zero healthcare or medical signals.

Important: if there is any healthcare/medical signal at all in either the name or the description, return "true". Do not return "uncertain" when medical or health is mentioned anywhere.

Respond ONLY with JSON: {"is_healthcare": "true" | "false" | "uncertain"}"""


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


def classify_one(az_client, company, description, attempt=0):
    user_content = f"Company: {company}\nDescription: {description[:600]}"
    try:
        resp = az_client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=30,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        val = data.get("is_healthcare", "uncertain").strip().lower()
        return val if val in {"true", "false", "uncertain"} else "uncertain"
    except Exception:
        if attempt < 2:
            time.sleep(2 ** attempt)
            return classify_one(az_client, company, description, attempt + 1)
        return "uncertain"


def main():
    ap = argparse.ArgumentParser(description="Classify agencies as healthcare/not → col U")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Reprocess rows already classified in col U")
    args = ap.parse_args()

    if not (AZURE_ENDPOINT and AZURE_API_KEY):
        print("ERROR: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set")
        return

    az_client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

    print("=== Classify Agencies — is_healthcare → col U ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col U exists
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 21:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 21 - col_count,
            }}]}
        ).execute()

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!U1",
        valueInputOption="RAW",
        body={"values": [["is_healthcare"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:U"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    pending = []
    for i, row in enumerate(data_rows):
        if args.limit and len(pending) >= args.limit:
            break
        if cell(row, COL_IS_HEALTHCARE) and not args.overwrite:
            continue  # already classified
        company = cell(row, COL_COMPANY)
        if not company:
            continue
        pending.append({
            "sheet_row": i + 2,
            "company": company,
            "description": cell(row, COL_DESC),
        })

    print(f"Rows to classify: {len(pending)}\n")

    if args.dry_run:
        for p in pending[:10]:
            print(f"  Row {p['sheet_row']}: {p['company'][:60]}")
        print("\n[DRY RUN] No API calls.")
        return

    counts = {"true": 0, "false": 0, "uncertain": 0}
    batches = [pending[b:b + BATCH_SIZE] for b in range(0, len(pending), BATCH_SIZE)]
    total_done = 0

    for idx, batch in enumerate(batches):
        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {
                pool.submit(classify_one, az_client, lead["company"], lead["description"]): lead
                for lead in batch
            }
            for fut in as_completed(futs):
                lead = futs[fut]
                val = fut.result()
                results[lead["sheet_row"]] = val
                counts[val] += 1

        updates = []
        for lead in batch:
            val = results[lead["sheet_row"]]
            updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_IS_HEALTHCARE)}{lead['sheet_row']}",
                "values": [[val]],
            })
            tag = "✓" if val == "true" else ("✗" if val == "false" else "?")
            print(f"  {tag} Row {lead['sheet_row']:4d}: {lead['company'][:55]}")

        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Write failed: {e}")

        time.sleep(0.3)
        total_done += len(batch)
        print(f"  [{idx + 1}/{len(batches)}] {total_done}/{len(pending)} done\n")

    print(f"=== Done — {total_done} rows classified ===")
    print(f"  healthcare (true):    {counts['true']}")
    print(f"  not healthcare (false): {counts['false']}")
    print(f"  uncertain:            {counts['uncertain']}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
