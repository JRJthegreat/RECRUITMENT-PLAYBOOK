"""
Find decision makers for healthcare staffing agencies (Sales Navigator sheet)
via AnyMail Finder decision-maker endpoint.

Reads:  col A (companyName), col K (website)
Writes: col L (dm_first_name), col M (dm_last_name), col N (dm_email),
        col O (dm_title), col P (dm_linkedin)

Resume-safe: skips rows where col N already has a value.
Parallel: 10 workers, writes to sheet every 40 rows.
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

ANYMAILFINDER_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

MAX_WORKERS = 10
BATCH_SIZE = 40

# Sales Navigator sheet column layout
COL_COMPANY  = 0   # A
COL_WEBSITE  = 10  # K
COL_DM_FIRST = 11  # L
COL_DM_LAST  = 12  # M
COL_EMAIL    = 13  # N
COL_DM_TITLE = 14  # O
COL_DM_LI    = 15  # P

# For staffing agencies: always target CEO/Founder first, fall back to HR
DM_CATEGORIES = ("ceo", "hr")


# ── AnyMail Finder ────────────────────────────────────────────────────────────

def _amf_headers():
    return {"Authorization": ANYMAILFINDER_API_KEY, "Content-Type": "application/json"}


def _extract_domain(value):
    if not value:
        return None
    d = value.strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].strip().lower()
    return d if d and "." in d and "linkedin.com" not in d else None


def _find_dm_one_call(domain, company_name, category):
    body = {"decision_maker_category": [category]}
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name
    if not domain and not company_name:
        return {"status": "missing_data"}
    try:
        resp = requests.post(AMF_DM_URL, headers=_amf_headers(), json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("valid_email") or data.get("email")
        status = data.get("email_status", "unknown")
        return {
            "email": email if email and status in ("valid", "risky") else None,
            "status": status or "not_found",
            "person_name": data.get("person_full_name", "") or "",
            "person_title": data.get("person_job_title", "") or "",
            "person_linkedin": data.get("person_linkedin_url", "") or "",
        }
    except requests.exceptions.HTTPError as e:
        return {"status": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"status": f"error:{type(e).__name__}"}


def _find_dm(domain, company_name):
    """Try DM_CATEGORIES in order; return first result that has a person name."""
    last = None
    for cat in DM_CATEGORIES:
        r = _find_dm_one_call(domain, company_name, cat)
        if r.get("person_name"):
            return r
        last = r
    return last or {"status": "not_found"}


def process_lead(lead):
    domain = _extract_domain(lead["website"])
    result = _find_dm(domain, lead["company"])
    name = result.get("person_name", "")
    parts = name.split(None, 1) if name else []
    return {
        **lead,
        "dm_first": parts[0] if parts else "",
        "dm_last": parts[1] if len(parts) > 1 else "",
        "email": result.get("email") or "",
        "title": result.get("person_title") or "",
        "linkedin": result.get("person_linkedin") or "",
        "status": result.get("status", ""),
    }


# ── Google Sheets ─────────────────────────────────────────────────────────────

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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


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


def ensure_columns(service, sheet_id, sheet_gid, min_cols=16):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < min_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": min_cols - col_count,
            }}]}
        ).execute()


def write_headers(service, sheet_id, tab_name):
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab_name}'!L1", "values": [["dm_first_name"]]},
            {"range": f"'{tab_name}'!M1", "values": [["dm_last_name"]]},
            {"range": f"'{tab_name}'!N1", "values": [["dm_email"]]},
            {"range": f"'{tab_name}'!O1", "values": [["dm_title"]]},
            {"range": f"'{tab_name}'!P1", "values": [["dm_linkedin"]]},
        ]},
    ).execute()


def flush_batch(service, sheet_id, tab_name, results):
    updates = []
    for r in results:
        rn = r["row_num"]
        for col, val in [
            (COL_DM_FIRST, r["dm_first"]),
            (COL_DM_LAST,  r["dm_last"]),
            (COL_EMAIL,    r["email"] or "not_found"),
            (COL_DM_TITLE, r["title"]),
            (COL_DM_LI,    r["linkedin"]),
        ]:
            updates.append({
                "range": f"'{tab_name}'!{col_letter(col)}{rn}",
                "values": [[val]],
            })
    for attempt in range(3):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                print(f"  [!] Sheet write failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Find DMs for Sales Navigator agencies via AnyMail Finder")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    ap.add_argument("--stop_at", type=int, default=0, help="Stop once this many total found contacts reached")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not ANYMAILFINDER_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set in .env")
        return

    print("=== Find DMs — Healthcare Staffing Agencies ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    ensure_columns(svc, sheet_id, sheet_gid, min_cols=16)
    write_headers(svc, sheet_id, tab_name)

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:P"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    # Count already-found contacts
    already_found = sum(
        1 for r in data_rows
        if len(r) > COL_EMAIL and r[COL_EMAIL].strip() and r[COL_EMAIL].strip() != "not_found"
    )
    print(f"Already found: {already_found}")
    if args.stop_at:
        print(f"Target:        {args.stop_at}")

    leads = []
    for i, row in enumerate(data_rows):
        if args.limit and len(leads) >= args.limit:
            break
        email_val = row[COL_EMAIL].strip() if len(row) > COL_EMAIL else ""
        if email_val:
            continue  # already processed
        website = row[COL_WEBSITE].strip() if len(row) > COL_WEBSITE else ""
        if not website:
            continue  # no domain → skip, AMF needs a domain for reliable results
        company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        leads.append({"row_num": i + 2, "company": company, "website": website})

    print(f"Rows to process: {len(leads)}\n")

    if args.dry_run:
        for lead in leads[:10]:
            domain = _extract_domain(lead["website"]) or "(no domain)"
            print(f"  Row {lead['row_num']}: {lead['company'][:50]:<50} → {domain}")
        print(f"\n[DRY RUN] No API calls.")
        return

    found = 0
    not_found = 0
    total_found = already_found  # track grand total including prior runs
    buffer = []
    stop_triggered = False

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures_map = {executor.submit(process_lead, lead): lead for lead in leads}

        completed = 0
        for future in as_completed(futures_map):
            completed += 1
            result = future.result()
            buffer.append(result)

            tag = f"[{result['status']}]" if not result["email"] else "[found]"
            name = f"{result['dm_first']} {result['dm_last']}".strip() or "-"
            print(f"  Row {result['row_num']:4d}: {result['company'][:40]:<40} {tag} {name} | {result['email'] or ''}")

            if result["email"]:
                found += 1
                total_found += 1
            else:
                not_found += 1

            if len(buffer) >= BATCH_SIZE or completed == len(leads):
                flush_batch(svc, sheet_id, tab_name, buffer)
                print(f"  → Wrote {completed}/{len(leads)} | this run: {found} found | total: {total_found}")
                buffer = []
                time.sleep(0.3)

            if args.stop_at and total_found >= args.stop_at:
                print(f"\n  ✓ Reached target of {args.stop_at} contacts. Stopping.")
                stop_triggered = True
                executor.shutdown(wait=False, cancel_futures=True)
                break

    if buffer:
        flush_batch(svc, sheet_id, tab_name, buffer)

    print(f"\n=== Done ===")
    print(f"  This run — found: {found}, not found: {not_found}")
    print(f"  Total contacts in sheet: {total_found}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
