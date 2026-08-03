"""
Split dm_name (col O) into first_name (col T) and last_name (col U) using
Azure OpenAI GPT-4.1.

The LLM handles titles (Dr./Mr.), suffixes (Jr./Sr./III), and post-nominals
(MD/RN/PA) so the first name is a clean given name for the email greeting.

Reads:  col A (Company Name), col O (dm_name), col S (email_status)
Writes: col T (first_name), col U (last_name)

Only processes rows where email_status == "found".
Resume-safe: skips rows where col T already populated.

Run:
  python3 -W ignore split_dm_names.py --sheet_url "URL" --tab "TAB" [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
from openai import AzureOpenAI
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

COL_DM_NAME      = 14  # O
COL_EMAIL_STATUS = 18  # S
COL_FIRST_NAME   = 19  # T
COL_LAST_NAME    = 20  # U

LLM_BATCH   = 25
WRITE_BATCH = 10

SYSTEM = (
    "You split a person's full name into a first name and a last name for use in "
    "cold-email personalization. Drop honorifics (Dr., Mr., Ms.), drop generational "
    "suffixes (Jr., Sr., II, III) and post-nominals (MD, RN, PA, MBA) from both fields. "
    "first_name = the given name in the casual form a colleague would use — "
    "common nicknames only (William -> Will, Jennifer -> Jen, Michael -> Mike); "
    "keep the original when no common nickname exists, never invent. "
    "last_name = the family name only. "
    "If only one token exists, last_name is empty. "
    "Return strict JSON: {\"results\":[{\"i\":<index>,\"first\":\"\",\"last\":\"\"}, ...]}."
)


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


def ensure_headers(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed = COL_LAST_NAME + 1
    if current_cols < needed:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": needed - current_cols,
            }}]},
        ).execute()
    for col_idx, header in [(COL_FIRST_NAME, "first_name"), (COL_LAST_NAME, "last_name")]:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!{col_letter(col_idx)}1",
            valueInputOption="RAW", body={"values": [[header]]},
        ).execute()


def split_batch(client, names):
    """names: list of (i, name). Returns {i: (first, last)}."""
    listing = "\n".join(f"{i}: {n}" for i, n in names)
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=1500, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Split these names:\n{listing}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        out = {}
        for r in data.get("results", []):
            out[int(r["i"])] = (r.get("first", "").strip(), r.get("last", "").strip())
        return out
    except Exception as e:
        print(f"  LLM batch error: {e} — falling back to naive split", flush=True)
        out = {}
        for i, n in names:
            parts = n.split()
            out[i] = (parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else "")
        return out


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = []
    for u in updates:
        data.append({"range": f"'{tab_name}'!{col_letter(COL_FIRST_NAME)}{u['row']}", "values": [[u["first"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_LAST_NAME)}{u['row']}",  "values": [[u["last"]]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--preview", type=int, default=0)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    ensure_headers(service, sheet_id, tab)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:U"
    ).execute().get("values", [])[1:]

    pending = []  # (row_num, name)
    for i, row in enumerate(rows):
        status = row[COL_EMAIL_STATUS].strip().lower() if len(row) > COL_EMAIL_STATUS else ""
        name   = row[COL_DM_NAME].strip() if len(row) > COL_DM_NAME else ""
        first  = row[COL_FIRST_NAME].strip() if len(row) > COL_FIRST_NAME else ""
        if status != "found" or not name or first:
            continue
        pending.append((i + 2, name))
    if args.limit:
        pending = pending[:args.limit]

    print(f"=== Split DM Names (Azure GPT-4.1) — tab '{tab}' ===")
    print(f"Rows to process: {len(pending)}\n", flush=True)

    updates = []
    for b in range(0, len(pending), LLM_BATCH):
        chunk = pending[b:b + LLM_BATCH]
        idx_to_row = {idx: row_num for idx, (row_num, _) in enumerate(chunk)}
        names = [(idx, name) for idx, (_, name) in enumerate(chunk)]
        result = split_batch(client, names)
        for idx, (row_num, name) in enumerate(chunk):
            first, last = result.get(idx, (name.split()[0] if name.split() else "", ""))
            if args.preview:
                print(f"  {name:35s} -> first={first!r} last={last!r}")
            updates.append({"row": row_num, "first": first, "last": last})
        if not args.preview and len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab); updates = []
        if args.preview and b + LLM_BATCH >= args.preview:
            break

    if args.preview:
        print("\n[PREVIEW] No writes.")
        return
    if updates:
        flush(service, updates, sheet_id, tab)
    print(f"\nDone — {len(pending)} names split.")


if __name__ == "__main__":
    main()
