"""
Phase 3: Find DMs and/or emails via AnyMail Finder (HR pipeline).

Replaces the previous Connector OS implementation. Two modes processed in
one pass:

  Mode A (person lookup) — row has DM Name (col T) but no Email (col W):
    POST /v5.1/find-email/person  with full_name + domain
    Writes email → W.

  Mode B (decision-maker lookup) — row has no DM Name but col L has a domain:
    POST /v5.1/find-email/decision-maker  with domain + category
    Tier (from find_dm.determine_target) → AMF category (primary, fallback):
      ceo         → ceo  → hr
      vp_hr       → hr   → ceo
      hr_manager  → hr   → ceo
    Writes name → T, title → U, linkedin → V, email → W.

AMF auth: "Authorization: {API_KEY}" (no "Bearer").
Accepts email_status in (valid, risky). "not_found" is written back to W so
re-runs skip the row (resume-safe).
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
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

MAX_WORKERS = 20
BATCH_SIZE = 40
SHEET_WRITE_DELAY = 0.3
TAB_NAME = "Leads"

# HR sheet layout
COL_JOB_TITLE = 1         # B
COL_COMPANY_NAME = 10     # K
COL_COMPANY_WEBSITE = 11  # L
COL_COMPANY_SIZE = 12     # M
COL_DM_NAME = 19          # T
COL_DM_TITLE = 20         # U
COL_LINKEDIN_URL = 21     # V
COL_EMAIL = 22            # W

TIER_TO_CATEGORIES = {
    "ceo":        ("ceo", "hr"),
    "vp_hr":      ("hr", "ceo"),
    "hr_manager": ("hr", "ceo"),
}


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


def split_name(full_name):
    name = (full_name or "").strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0], parts[0]
    parts = name.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


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


def categories_for(job_title, company_size):
    tier, _ = determine_target(job_title, company_size)
    return TIER_TO_CATEGORIES.get(tier, ("hr", "ceo"))


def find_email_person(full_name, domain, company_name):
    first_name, last_name = split_name(full_name)
    headers = {"Authorization": ANYMAILFINDER_API_KEY, "Content-Type": "application/json"}
    body = {}
    if full_name:
        body["full_name"] = full_name
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name

    has_name = full_name or (first_name and last_name)
    has_company = domain or company_name
    if not has_name or not has_company:
        return {"email": None, "status": "missing_data"}

    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        if email and status in ("valid", "risky"):
            return {"email": email, "status": status}
        return {"email": None, "status": status or "not_found"}
    except requests.exceptions.HTTPError as e:
        return {"email": None, "status": f"http_{e.response.status_code}"}
    except Exception:
        return {"email": None, "status": "error"}


def find_dm_one_call(domain, company_name, category):
    headers = {"Authorization": ANYMAILFINDER_API_KEY, "Content-Type": "application/json"}
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


def find_dm_with_fallback(domain, company_name, primary, fallback):
    r1 = find_dm_one_call(domain, company_name, primary)
    if r1.get("person_name"):
        return r1
    r2 = find_dm_one_call(domain, company_name, fallback)
    if r2.get("person_name"):
        return r2
    r1_err = (r1.get("status", "") or "").startswith(("http_", "error"))
    r2_err = (r2.get("status", "") or "").startswith(("http_", "error"))
    if r1_err and not r2_err:
        return r2
    return r1


def process_person(lead):
    domain = extract_domain(lead["company_website"])
    result = find_email_person(lead["dm_name"], domain, lead["company_name"])
    return {**lead, **result}


def process_dm(lead):
    domain = extract_domain(lead["company_website"])
    primary, fallback = categories_for(lead["job_title"], lead["company_size"])
    result = find_dm_with_fallback(domain, lead["company_name"], primary, fallback)
    return {**lead, **result, "primary": primary, "fallback": fallback}


def batch_write(service, sheet_id, updates):
    if not updates:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="AMF: enrich emails (known DMs) + find DMs (missing rows)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=0, help="Max leads across both modes (0 = all)")
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--person_only", action="store_true", help="Only Mode A (email for known DMs)")
    parser.add_argument("--dm_only", action="store_true", help="Only Mode B (find DM for missing rows)")
    parser.add_argument("--retry_not_found", action="store_true", help="Retry rows with email='not_found'")
    args = parser.parse_args()

    if not ANYMAILFINDER_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set in .env")
        return

    print("=== Enrich via AnyMail Finder ===\n")
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
    print(f"  {len(data_rows)} data rows\n")

    person_leads, dm_leads = [], []
    for i, row in enumerate(data_rows):
        sr = i + 2
        email = cell(row, COL_EMAIL).lower()
        dm_name = cell(row, COL_DM_NAME)

        if email and email != "not_found":
            continue
        if email == "not_found" and not args.retry_not_found:
            continue

        website = cell(row, COL_COMPANY_WEBSITE)
        company_name = cell(row, COL_COMPANY_NAME)
        size = cell(row, COL_COMPANY_SIZE)
        job_title = cell(row, COL_JOB_TITLE)

        domain = extract_domain(website)
        if not domain and not company_name:
            continue

        if dm_name and not args.dm_only:
            person_leads.append({
                "sheet_row": sr,
                "dm_name": dm_name,
                "company_name": company_name,
                "company_website": website,
            })
        elif not dm_name and not args.person_only:
            if not domain:
                continue  # AMF /decision-maker needs a domain to be useful
            dm_leads.append({
                "sheet_row": sr,
                "company_name": company_name,
                "company_website": website,
                "company_size": size,
                "job_title": job_title,
            })

    if args.limit:
        person_leads = person_leads[:args.limit]
        dm_leads = dm_leads[:max(0, args.limit - len(person_leads))]

    print(f"  Mode A (person -> email): {len(person_leads)}")
    print(f"  Mode B (find DM):         {len(dm_leads)}")
    print(f"  Total AMF calls ~= {len(person_leads) + 2 * len(dm_leads)} (DM mode uses 2 calls worst case)\n")

    if args.dry_run:
        print("[DRY RUN] First 10 of each mode:")
        for l in person_leads[:10]:
            first, last = split_name(l["dm_name"])
            dom = extract_domain(l["company_website"])
            print(f"  A row {l['sheet_row']}: {first} {last} @ {dom or '?'}")
        for l in dm_leads[:10]:
            p, f = categories_for(l["job_title"], l["company_size"])
            dom = extract_domain(l["company_website"])
            print(f"  B row {l['sheet_row']}: {l['company_name']!r} @ {dom or '?'} size={l['company_size']!r} -> {p}/{f}")
        return

    total_email_found = total_email_miss = 0
    total_dm_found = total_dm_miss = 0

    if person_leads:
        batches = (len(person_leads) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"--- Mode A: person email ({batches} batches) ---\n")
        for b in range(batches):
            batch = person_leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
            print(f"  Batch A{b + 1}/{batches}")
            updates = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(process_person, l): l for l in batch}
                for fut in as_completed(futures):
                    r = fut.result()
                    sr = r["sheet_row"]
                    if r.get("email"):
                        note = f" [{r['status']}]" if r["status"] == "risky" else ""
                        print(f"    row {sr}: {r['dm_name']} -> {r['email']}{note}")
                        updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}", "values": [[r["email"]]]})
                        total_email_found += 1
                    else:
                        print(f"    row {sr}: {r['dm_name']} -> not_found ({r['status']})")
                        updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}", "values": [["not_found"]]})
                        total_email_miss += 1
            batch_write(service, sheet_id, updates)
            time.sleep(SHEET_WRITE_DELAY)

    if dm_leads:
        batches = (len(dm_leads) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n--- Mode B: find DM ({batches} batches) ---\n")
        for b in range(batches):
            batch = dm_leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
            print(f"  Batch B{b + 1}/{batches}")
            updates = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(process_dm, l): l for l in batch}
                for fut in as_completed(futures):
                    r = fut.result()
                    sr = r["sheet_row"]
                    name = r.get("person_name") or ""
                    email = r.get("email") or ""
                    title = r.get("person_title") or ""
                    li = r.get("person_linkedin") or ""
                    cat = r.get("category_used", "?")
                    tag = f"[{cat}]"

                    if name:
                        print(f"    row {sr} {tag}: {name} <{email or 'no-email'}> -- {title} -- {r['company_name']}")
                        updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_DM_NAME)}{sr}", "values": [[name]]})
                        if title:
                            updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_DM_TITLE)}{sr}", "values": [[title]]})
                        if li:
                            updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_LINKEDIN_URL)}{sr}", "values": [[li]]})
                        if email:
                            updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}", "values": [[email]]})
                            total_dm_found += 1
                        else:
                            updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}", "values": [["not_found"]]})
                            total_dm_miss += 1
                    else:
                        print(f"    row {sr} {tag}: no DM found ({r.get('status', '?')}) -- {r['company_name']}")
                        updates.append({"range": f"'{TAB_NAME}'!{col_letter(COL_EMAIL)}{sr}", "values": [["not_found"]]})
                        total_dm_miss += 1
            batch_write(service, sheet_id, updates)
            time.sleep(SHEET_WRITE_DELAY)

    print(f"\n=== Done ===")
    print(f"  Mode A emails: {total_email_found} found / {total_email_miss} not found")
    print(f"  Mode B DMs:    {total_dm_found} found+email / {total_dm_miss} not found")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
