"""
Generate personalized email bodies for healthcare staffing agency contacts.

Reads:  col A (companyName), col B (description), col N (dm_email)
Writes: col S (email_body)

Only processes rows where col N has a valid email.
Resume-safe: skips rows where col S already populated.
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
MAX_RETRIES = 3

COL_COMPANY  = 0   # A
COL_DESC     = 1   # B
COL_EMAIL    = 13  # N
COL_BODY     = 18  # S

VALID_ROLES = {"physician", "nurse practitioner"}

SYSTEM_PROMPT = """You are a B2B outreach specialist. Given a healthcare staffing company's name and description, extract two fields for a cold email:

1. "icp" — the specific type of healthcare organizations they staff. Target is small, independent employers — NOT hospitals or health systems.
   Good examples: "private medical practices", "independent outpatient clinics", "small healthcare employers", "specialty practices", "private dental practices", "behavioral health practices".
   FORBIDDEN: do NOT mention hospitals, health systems, medical centers, or any specific city/state/region.
   If the description only mentions hospitals or health systems, default to "private medical practices".
   Never include a location name in the icp value.

2. "role" — MUST be exactly one of these two values:
   - "physician" — if the description mentions placing physicians, doctors, MDs, DOs, hospitalists, locum tenens, or any medical doctor role
   - "nurse practitioner" — in all other cases (NPs, RNs, PAs, CNAs, nurses, etc.)

Rules:
- icp must be specific, location-agnostic, and focused on private/independent employers.
- role must be exactly "physician" or "nurse practitioner" — no other values.
- No hallucination.

Respond ONLY with JSON: {"icp": "...", "role": "physician" | "nurse practitioner"}"""

TEMPLATE = (
    "I stumbled across your work helping {icp} fill {role} roles. Impressive stuff.\n\n"
    "I'm connected with a few {icp} currently struggling to fill {role} roles and open "
    "to working with external recruiters.\n\n"
    "Could intro you if you're looking for fresh reqs.\n\n"
    "Worth a quick chat?\n\n"
    "Best,\n"
    "Jude"
)


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


def extract_icp_role(az_client, company_name, description, attempt=0):
    user_content = f"Company: {company_name}\nDescription: {description[:500]}"
    try:
        resp = az_client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=80,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        icp = data.get("icp", "").strip() or "medical practices"
        role = data.get("role", "").strip().lower()
        if role not in VALID_ROLES:
            role = "nurse practitioner"
        return icp, role
    except Exception as e:
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)
            return extract_icp_role(az_client, company_name, description, attempt + 1)
        return "medical practices", "nurse practitioner"


def process_lead(az_client, lead):
    icp, role = extract_icp_role(az_client, lead["company"], lead["description"])
    body = TEMPLATE.format(icp=icp, role=role)
    return {**lead, "icp": icp, "role": role, "body": body}


def main():
    ap = argparse.ArgumentParser(description="Generate email bodies for agency contacts → col S")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Reprocess rows already written to col S")
    args = ap.parse_args()

    if not (AZURE_ENDPOINT and AZURE_API_KEY):
        print("ERROR: AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set")
        return

    az_client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

    print("=== Generate Agency Email Bodies ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col S exists
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 19:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 19 - col_count,
            }}]}
        ).execute()

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!S1",
        valueInputOption="RAW",
        body={"values": [["email_body"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:S"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        email = cell(row, COL_EMAIL)
        if not email or email.lower() == "not_found":
            continue
        if cell(row, COL_BODY) and not args.overwrite:
            continue
        company = cell(row, COL_COMPANY)
        desc = cell(row, COL_DESC)
        if not company:
            continue
        leads.append({
            "sheet_row": i + 2,
            "company": company,
            "description": desc,
            "email": email,
        })

    print(f"Rows to process: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:5]:
            icp, role = extract_icp_role(az_client, lead["company"], lead["description"])
            body = TEMPLATE.format(icp=icp, role=role)
            print(f"  Row {lead['sheet_row']} ({lead['company']}):")
            print(f"  icp={icp!r}  role={role!r}")
            print(f"  {body}")
            print()
        print("[DRY RUN] No writes.")
        return

    batches = [leads[b:b + BATCH_SIZE] for b in range(0, len(leads), BATCH_SIZE)]
    total_done = 0

    for idx, batch in enumerate(batches):
        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(process_lead, az_client, lead): lead for lead in batch}
            for fut in as_completed(futs):
                r = fut.result()
                results[r["sheet_row"]] = r

        updates = []
        for lead in batch:
            r = results[lead["sheet_row"]]
            updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_BODY)}{lead['sheet_row']}",
                "values": [[r["body"]]],
            })
            print(f"  Row {lead['sheet_row']:4d}: {lead['company'][:40]:<40} [{r['role']}] icp={r['icp'][:30]!r}")

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
        print(f"  [{idx + 1}/{len(batches)}] {total_done}/{len(leads)} done\n")

    print(f"=== Done — {total_done} email bodies written to col S ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
