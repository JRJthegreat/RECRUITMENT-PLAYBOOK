"""
Phase 3.5: AMF rescue — fill DM Name + DM Title + LinkedIn URL + Email for
rows where Phase 2 (Google Search) couldn't find a DM.

Runs after Phase 3 (Connector OS for known DMs) so we exhaust the cheap
options first and only spend AMF credits on the leftover gap.

Filter: DM Name empty AND col L has a domain (Phase 1.92 populated it).
For each row, reuse Phase 2's tier logic to pick an AMF decision-maker
category, call /find-email/decision-maker, and write back name/title/email.
LinkedIn URL written when AMF returns one. If email status is 'not_found'
we still write that to col W so the next run skips the row (resume-safe).

AMF auth: Authorization: {API_KEY}  (no 'Bearer').
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

from find_dm import determine_target

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

ANYMAILFINDER_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

MAX_WORKERS = 10
BATCH_SIZE = 20
SHEET_WRITE_DELAY = 0.5
TAB_NAME = "Leads"

COL_JOB_TITLE = 1         # B
COL_COMPANY_NAME = 10     # K
COL_COMPANY_WEBSITE = 11  # L
COL_COMPANY_SIZE = 12     # M
COL_DM_NAME = 19          # T
COL_DM_TITLE = 20         # U
COL_LINKEDIN_URL = 21     # V
COL_EMAIL = 22            # W


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def extract_domain(value):
    if not value:
        return None
    if "linkedin.com" in value.lower():
        return None
    d = value.strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].strip().lower()
    if not d or "." not in d:
        return None
    return d


# Map our tier (from determine_target) → AMF decision_maker_category.
# AMF-valid: ceo, engineering, finance, hr, it, logistics, marketing,
# operations, buyer, sales.
TIER_TO_CATEGORIES = {
    "ceo":       ("ceo", "hr"),
    "senior_hr": ("hr", "ceo"),
}


def categories_for(job_title, company_size):
    tier, _ = determine_target(job_title, company_size)
    return TIER_TO_CATEGORIES.get(tier, ("hr", "ceo"))


def find_dm_amf(domain, company_name, category):
    headers = {
        "Authorization": ANYMAILFINDER_API_KEY,
        "Content-Type": "application/json",
    }
    body = {"decision_maker_category": [category]}
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name
    if not domain and not company_name:
        return {"status": "missing_data"}

    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=180)
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
            "category_used": category,
        }
    except requests.exceptions.HTTPError as e:
        return {"status": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"status": f"error: {type(e).__name__}"}


def find_with_fallback(domain, company_name, primary, fallback):
    r1 = find_dm_amf(domain, company_name, primary)
    if r1.get("person_name"):
        return r1
    r2 = find_dm_amf(domain, company_name, fallback)
    if r2.get("person_name"):
        return r2
    r1_err = (r1.get("status", "") or "").startswith(("http_", "error"))
    r2_err = (r2.get("status", "") or "").startswith(("http_", "error"))
    if r1_err and not r2_err:
        return r2
    return r1


def process_one(lead):
    primary, fallback = categories_for(lead["job_title"], lead["company_size"])
    result = find_with_fallback(
        lead["domain"], lead["company_name"], primary, fallback
    )
    return {**lead, **result, "primary": primary, "fallback": fallback}


def batch_write(service, sheet_id, updates):
    if not updates:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()


def main():
    ap = argparse.ArgumentParser(description="Phase 3.5: AMF rescue for rows missing a DM")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--retry_not_found", action="store_true",
                    help="Re-attempt rows previously marked 'not_found' in col W")
    args = ap.parse_args()

    if not ANYMAILFINDER_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set in .env")
        return

    print("=== Phase 3.5: AMF DM Rescue ===\n")
    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A:AC"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("  No data rows.")
        return
    data_rows = all_rows[1:]
    print(f"  Total data rows: {len(data_rows)}")

    leads = []
    for i, row in enumerate(data_rows):
        if cell(row, COL_DM_NAME):
            continue
        domain = extract_domain(cell(row, COL_COMPANY_WEBSITE))
        if not domain:
            continue
        existing_email = cell(row, COL_EMAIL).lower()
        if existing_email and existing_email != "not_found" and not args.retry_not_found:
            continue
        if existing_email == "not_found" and not args.retry_not_found:
            continue
        leads.append({
            "sheet_row": i + 2,
            "job_title": cell(row, COL_JOB_TITLE),
            "company_name": cell(row, COL_COMPANY_NAME),
            "company_size": cell(row, COL_COMPANY_SIZE),
            "domain": domain,
        })
        if args.limit and len(leads) >= args.limit:
            break

    print(f"  Eligible leads (no DM, has domain): {len(leads)}\n")
    if not leads:
        print("Nothing to do.")
        return

    if args.dry_run:
        for lead in leads[:25]:
            primary, fallback = categories_for(lead["job_title"], lead["company_size"])
            print(f"  Row {lead['sheet_row']}: {lead['company_name']} "
                  f"({lead['domain']}) → {primary}/{fallback}")
        print(f"\n[DRY RUN] Would call AMF for {len(leads)} rows.")
        return

    found = 0
    not_found = 0
    errors = 0
    num_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    t0 = time.time()

    for b in range(num_batches):
        chunk = leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"Batch {b + 1}/{num_batches} ({len(chunk)} leads)")

        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(process_one, lead): lead for lead in chunk}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    lead = futs[fut]
                    print(f"  [!] Row {lead['sheet_row']} crashed: {e}")
                    results.append({**lead, "status": f"crash: {type(e).__name__}"})

        updates = []
        for r in results:
            row = r["sheet_row"]
            person_name = r.get("person_name") or ""
            email = r.get("email") or ""
            status = r.get("status", "")

            if person_name:
                updates.append({
                    "range": f"'{TAB_NAME}'!{col_letter(COL_DM_NAME)}{row}",
                    "values": [[person_name]],
                })
                if r.get("person_title"):
                    updates.append({
                        "range": f"'{TAB_NAME}'!{col_letter(COL_DM_TITLE)}{row}",
                        "values": [[r["person_title"]]],
                    })
                if r.get("person_linkedin"):
                    updates.append({
                        "range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN_URL)}{row}",
                        "values": [[r["person_linkedin"]]],
                    })

            if email:
                updates.append({
                    "range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{row}",
                    "values": [[email]],
                })
                found += 1
                print(f"  Row {row}: {r['company_name']} → {person_name} "
                      f"({r.get('person_title','')}) | {email} "
                      f"[{r.get('category_used') or r.get('primary')}]")
            else:
                if status not in ("missing_data",) and not status.startswith(("http_", "error", "crash")):
                    updates.append({
                        "range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{row}",
                        "values": [["not_found"]],
                    })
                    not_found += 1
                    print(f"  Row {row}: {r['company_name']} → no email (status={status})")
                else:
                    errors += 1
                    print(f"  Row {row}: {r['company_name']} → ERROR ({status})")

        batch_write(service, sheet_id, updates)
        time.sleep(SHEET_WRITE_DELAY)

    elapsed = int(time.time() - t0)
    print("\n=== Summary ===")
    print(f"Leads attempted: {len(leads)}")
    print(f"  Email found:   {found}")
    print(f"  No email:      {not_found}")
    print(f"  Errors:        {errors}")
    print(f"Elapsed:         {elapsed}s")
    print(f"Sheet:           https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
