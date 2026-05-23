"""
Phase 3: Find decision maker (CEO/Founder) name, title, and email.

Step A (Primary — ~$0.015/company):
  AnyMail Finder /find-email/decision-maker with company domain.
  Source: website col M → company_domain col C as fallback.

Step B (Fallback — ~$0.015/company):
  Google Search → parse CEO name from result title → AnyMail Finder /find-email/person.
  Only runs for rows still missing a DM after Step A.

Writes:
  - founder_name (DM name) → col F (index 5)
  - dm_title              → col S (index 18)
  - dm_email              → col T (index 19)  "not_found" written when AMF finds nothing.

Run:
  python3 -W ignore find_ceo.py --sheet_url "URL" [--limit N]
"""

import os
import re
import json
import time
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"
APIFY_GOOGLE = "apify~google-search-scraper"

AMF_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_NAME        = 2   # C: company_name
COL_DM_NAME     = 4   # E: founder_name
COL_WEBSITE     = 11  # L: website
COL_DM_TITLE    = 17  # R: dm_title
COL_DM_EMAIL    = 18  # S: dm_email
COL_DM_LINKEDIN = 19  # T: dm_linkedin_url

WRITE_BATCH = 10
GOOGLE_BATCH = 50
AMF_WORKERS = 8

CEO_KEYWORDS = [
    "ceo", "chief executive", "founder", "co-founder", "cofounder",
    "president", "owner", "managing director", "managing partner",
    "principal", "general manager",
]


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


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


def ensure_headers(service):
    # Get current sheet dimensions and expand if needed
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == TAB_NAME)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = max(COL_DM_TITLE, COL_DM_EMAIL, COL_DM_LINKEDIN) + 2
    if current_cols < needed_cols:
        sheet_id = sheet["properties"]["sheetId"]
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
        print(f"  Expanded sheet to {needed_cols} columns")

    for col_idx, header in [(COL_DM_TITLE, "dm_title"), (COL_DM_EMAIL, "dm_email"), (COL_DM_LINKEDIN, "dm_linkedin_url")]:
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{TAB_NAME}'!{col_letter(col_idx)}1",
            valueInputOption="RAW",
            body={"values": [[header]]},
        ).execute()
        print(f"  Set header: {header}")


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def flush_updates(service, updates):
    if not updates:
        return
    data = []
    for u in updates:
        if u.get("dm_name"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_DM_NAME)}{u['row']}",
                "values": [[u["dm_name"]]],
            })
        if u.get("dm_title"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_DM_TITLE)}{u['row']}",
                "values": [[u["dm_title"]]],
            })
        if u.get("dm_linkedin"):
            data.append({
                "range": f"'{TAB_NAME}'!{col_letter(COL_DM_LINKEDIN)}{u['row']}",
                "values": [[u["dm_linkedin"]]],
            })
        # Always write dm_email (even "not_found") so re-runs skip the row
        data.append({
            "range": f"'{TAB_NAME}'!{col_letter(COL_DM_EMAIL)}{u['row']}",
            "values": [[u.get("dm_email", "not_found")]],
        })
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
        ).execute()
    print(f"  -> Wrote {len(updates)} DMs", flush=True)
    time.sleep(1)


def amf_decision_maker(domain, company_name):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"decision_maker_category": ["ceo"]}
    if domain:
        body["domain"] = domain
    if company_name:
        body["company_name"] = company_name
    try:
        resp = requests.post(AMF_DM_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        status = d.get("email_status", "")
        if status not in ("valid", "risky"):
            email = None
        return {
            "dm_name": d.get("person_full_name", "") or "",
            "dm_title": d.get("person_job_title", "") or "",
            "dm_email": email or "",
            "dm_linkedin": d.get("person_linkedin_url", "") or "",
        }
    except Exception as e:
        return {"dm_name": "", "dm_title": "", "dm_email": "", "dm_linkedin": "", "error": str(e)}


def amf_person(full_name, domain, company_name):
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    parts = full_name.strip().split(None, 1)
    body = {"full_name": full_name, "domain": domain}
    if len(parts) >= 1:
        body["first_name"] = parts[0]
    if len(parts) >= 2:
        body["last_name"] = parts[1]
    if company_name:
        body["company_name"] = company_name
    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("valid_email") or d.get("email")
        status = d.get("email_status", "")
        if status not in ("valid", "risky"):
            email = None
        return email or ""
    except Exception:
        return ""


def apify_google_search(queries):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_GOOGLE}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"queries": "\n".join(queries), "resultsPerPage": 5,
                  "maxPagesPerQuery": 1, "languageCode": "en",
                  "countryCode": "us", "includeUnfilteredResults": False},
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"  [!] Google search error: {e}")
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] HTTP {resp.status_code}: {resp.text[:200]}")
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def parse_ceo_from_results(organic):
    for r in organic:
        title = r.get("title", "")
        url = r.get("url", "")
        if "linkedin.com/in/" not in url.lower():
            continue
        linkedin_url = url.split("?")[0]
        title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
        parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
        if len(parts) >= 2:
            name = parts[0].strip()
            role = re.sub(r"\s+at\s+.*$", "", parts[1].strip(), flags=re.IGNORECASE).strip()
        else:
            p2 = title.split(",", 1)
            name = p2[0].strip()
            role = p2[1].strip() if len(p2) == 2 else ""
        if not name:
            continue
        role_lower = role.lower()
        if any(kw in role_lower for kw in CEO_KEYWORDS):
            return name, role, linkedin_url
    return "", "", ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", default=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not AMF_API_KEY:
        print("ERROR: ANYMAILFINDER_API_KEY not set"); return

    service = get_service()
    ensure_headers(service)

    print("=== Phase 3: Find Decision Maker (AMF) ===\n", flush=True)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    # Step A targets: no DM name, no dm_email already set, has some domain
    step_a = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        dm_name = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""
        dm_email = row[COL_DM_EMAIL] if len(row) > COL_DM_EMAIL else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        domain = extract_domain(website)
        if name.strip() and not dm_name.strip() and not dm_email.strip() and domain:
            step_a.append({
                "row": i + 2, "name": name.strip(), "domain": domain,
            })

    if args.limit:
        step_a = step_a[:args.limit]

    print(f"[Step A] AMF decision-maker lookup — {len(step_a)} companies...", flush=True)
    updates = []
    a_found = a_not_found = 0

    def run_amf_dm(t):
        return t, amf_decision_maker(t["domain"], t["name"])

    with ThreadPoolExecutor(max_workers=AMF_WORKERS) as ex:
        futures = [ex.submit(run_amf_dm, t) for t in step_a]
        for i, fut in enumerate(as_completed(futures), 1):
            t, result = fut.result()
            dm_name = result.get("dm_name", "")
            dm_email = result.get("dm_email", "")
            dm_linkedin = result.get("dm_linkedin", "")
            if dm_name or dm_email:
                a_found += 1
                print(f"  +  {t['name'][:50]:50s} → {dm_name} | {dm_email or '(no email)'}", flush=True)
            else:
                a_not_found += 1
            updates.append({
                "row": t["row"],
                "dm_name": dm_name,
                "dm_title": result.get("dm_title", ""),
                "dm_email": dm_email if dm_email else "not_found",
                "dm_linkedin": dm_linkedin,
            })
            if len(updates) >= WRITE_BATCH:
                flush_updates(service, updates)
                updates = []
            if i % 50 == 0:
                print(f"  Progress: {i}/{len(step_a)}", flush=True)

    if updates:
        flush_updates(service, updates)
    print(f"\n  Step A: found {a_found}, not found {a_not_found} / {len(step_a)}\n", flush=True)

    # Re-read for Step B: dm_name still empty (AMF DM returned no name)
    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    step_b = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        dm_name = row[COL_DM_NAME] if len(row) > COL_DM_NAME else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        domain = extract_domain(website)
        if name.strip() and not dm_name.strip() and domain:
            step_b.append({"row": i + 2, "name": name.strip(), "domain": domain})

    if args.limit:
        step_b = step_b[:args.limit]

    if not step_b:
        print("[Step B] All companies have a DM name — skipping.\n"); return

    print(f"[Step B] Google fallback — {len(step_b)} companies...", flush=True)
    queries = [f'"{t["name"]}" ("CEO" OR "Founder" OR "President" OR "Owner") site:linkedin.com/in/' for t in step_b]
    qmap = dict(zip(queries, step_b))
    total_batches = (len(queries) + GOOGLE_BATCH - 1) // GOOGLE_BATCH
    b_found = b_not_found = 0
    updates = []

    for b in range(0, len(queries), GOOGLE_BATCH):
        batch = queries[b:b + GOOGLE_BATCH]
        bn = b // GOOGLE_BATCH + 1
        print(f"  Google batch {bn}/{total_batches}...", flush=True)
        batch_results = apify_google_search(batch)

        for q in batch:
            t = qmap.get(q)
            if not t:
                continue
            organic = batch_results.get(q, [])
            ceo_name, ceo_title, ceo_linkedin = parse_ceo_from_results(organic)

            if ceo_name:
                email = amf_person(ceo_name, t["domain"], t["name"])
                if email:
                    b_found += 1
                    print(f"  +  {t['name'][:50]:50s} → {ceo_name} | {email}", flush=True)
                else:
                    b_not_found += 1
                    print(f"  ~  {t['name'][:50]:50s} → {ceo_name} (no email)", flush=True)
                updates.append({
                    "row": t["row"],
                    "dm_name": ceo_name,
                    "dm_title": ceo_title,
                    "dm_email": email if email else "not_found",
                    "dm_linkedin": ceo_linkedin,
                })
            else:
                b_not_found += 1
                updates.append({
                    "row": t["row"],
                    "dm_name": "",
                    "dm_title": "",
                    "dm_email": "not_found",
                    "dm_linkedin": "",
                })

            if len(updates) >= WRITE_BATCH:
                flush_updates(service, updates)
                updates = []

        print(f"  Batch {bn} done — {b_found} found so far", flush=True)

    if updates:
        flush_updates(service, updates)

    print(f"\nStep B: found {b_found}, not found {b_not_found} / {len(step_b)}")
    print(f"\nTotal: DM found {a_found + b_found} / {len(step_a) + len(step_b)}", flush=True)


if __name__ == "__main__":
    main()
