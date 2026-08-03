"""
Phase 0 - pull the decision maker's LinkedIn profile into the sheet.

LinkedIn is the highest-yield research source for this use case. A company
website may be boilerplate or WAF-blocked (~35% of the time), but a profile
that scrapes successfully ALWAYS carries `currentJobDuration`, so every hit
yields at least one concrete, checkable fact (tenure). `about` is the richer
seam: founder stories and self-published numbers ("250+ placements") live there.

Actor: dev_fusion~Linkedin-Profile-Scraper ($3 per 1,000 profiles).
Note supreme_coder~linkedin-profile-scraper returns nothing as of 2026-06,
which is why the other verify_dms.py scripts in this repo moved off it.

Writes a compacted JSON blob to --col_out with only the fields that matter for
research, so the cell stays readable and downstream parsing is stable.

Batch-of-10 sheet writes. Resume-safe: skips rows where --col_out is filled.
Profiles get blocked intermittently, same as websites, so rows that come back
empty are left blank and recovered by simply re-running.

Run:
  python3 -W ignore scrape_linkedin.py --sheet_url "URL" --tab "TAB" \
    --col_linkedin O --col_out Q [--limit N] [--preview N]
"""

import os
import re
import json
import time
import html
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ACTOR_ID    = "dev_fusion~Linkedin-Profile-Scraper"
SYNC_URL    = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

WRITE_BATCH = 10
BATCH_SIZE  = 20     # profile URLs per actor call
WORKERS     = 4      # concurrent actor runs
TIMEOUT     = 300


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


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


def clean(s, limit=1200):
    """about comes back HTML-escaped (&amp;). Unescape before it reaches copy."""
    s = html.unescape(str(s or "")).strip()
    s = s.replace("—", ", ").replace("–", ", ")
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def norm_url(u):
    u = (u or "").strip().lower().rstrip("/")
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^([a-z]{2,3}\.)?linkedin\.com", "linkedin.com", u)
    return u


def compact(item):
    """Keep only what research needs. Full payload is huge and mostly noise."""
    exps = []
    for e in (item.get("experiences") or [])[:6]:
        exps.append({
            "title": clean(e.get("title"), 120),
            "sub": clean(e.get("subtitle"), 120),
            "when": clean(e.get("caption"), 80),
        })
    edus = []
    for e in (item.get("educations") or [])[:3]:
        edus.append({
            "school": clean(e.get("title"), 100),
            "detail": clean(e.get("subtitle"), 100),
        })
    def names(key, limit=4):
        out = []
        for x in (item.get(key) or [])[:limit]:
            if isinstance(x, dict):
                out.append(clean(x.get("title") or x.get("name"), 120))
            elif isinstance(x, str):
                out.append(clean(x, 120))
        return [x for x in out if x]

    return {
        "name": clean(item.get("fullName"), 80),
        "title": clean(item.get("jobTitle"), 100),
        "headline": clean(item.get("headline"), 200),
        "about": clean(item.get("about"), 1500),
        "company": clean(item.get("companyName"), 120),
        "company_size": clean(item.get("companySize"), 20),
        "company_industry": clean(item.get("companyIndustry"), 80),
        # Independent read on the company domain. Settles rows where the
        # email domain and the sheet's website disagree.
        "company_website": clean(item.get("companyWebsite"), 120),
        "company_linkedin": clean(item.get("companyLinkedin"), 160),
        "company_founded": clean(item.get("companyFoundedIn"), 20),
        "location": clean(item.get("addressWithCountry"), 80),
        "tenure": clean(item.get("currentJobDuration"), 40),
        "started": clean(item.get("jobStartedOn"), 20),
        "first_role_year": item.get("firstRoleYear"),
        "still_employed": item.get("isCurrentlyEmployed"),
        # Feed the awards / credentials fact slots directly.
        "awards": names("honorsAndAwards"),
        "certifications": names("licenseAndCertificates"),
        "experiences": exps,
        "educations": edus,
    }


def run_actor(urls):
    try:
        r = requests.post(SYNC_URL, params={"token": APIFY_TOKEN},
                          json={"profileUrls": urls}, timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            return []
        return r.json()
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_linkedin", default="O")
    parser.add_argument("--col_out", default="Q")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--preview", type=int, default=0)
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    c_li  = col_to_idx(args.col_linkedin)
    c_out = col_to_idx(args.col_out)

    service = get_service()
    last = col_letter(max(c_li, c_out))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last}"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        li  = row[c_li].strip()  if len(row) > c_li  else ""
        out = row[c_out].strip() if len(row) > c_out else ""
        if not li or out:
            continue
        pending.append({"row": i + 2, "url": li})

    if args.limit:
        pending = pending[:args.limit]
    if args.preview:
        pending = pending[:args.preview]

    print(f"=== LinkedIn Profile Scrape ===\nActor: {ACTOR_ID}")
    print(f"Profiles to fetch: {len(pending)}  (~${len(pending)*3/1000:.2f})\n")
    if not pending:
        return

    # ensure output column exists
    if not args.preview:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sh = next(s for s in meta["sheets"] if s["properties"]["title"] == tab)
        cur = sh["properties"]["gridProperties"]["columnCount"]
        if cur < c_out + 1:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"appendDimension": {
                    "sheetId": sh["properties"]["sheetId"],
                    "dimension": "COLUMNS", "length": c_out + 1 - cur}}]}).execute()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{tab}'!{col_letter(c_out)}1",
            valueInputOption="RAW", body={"values": [["linkedin_data"]]}).execute()

    by_url = {norm_url(p["url"]): p["row"] for p in pending}
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

    updates, got = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(run_actor, [p["url"] for p in b]): b for b in batches}
        for fut in as_completed(futs):
            items = fut.result() or []
            for it in items:
                u = norm_url(it.get("linkedinUrl") or it.get("linkedinPublicUrl") or "")
                row = by_url.get(u)
                if row is None:
                    slug = (it.get("publicIdentifier") or "").strip().lower()
                    for k, v in by_url.items():
                        if slug and k.endswith("/in/" + slug):
                            row = v
                            break
                if row is None:
                    continue
                data = compact(it)
                got += 1
                if args.preview:
                    print(f"--- Row {row} | {data['name']} | {data['title']} ---")
                    print(f"  company : {data['company']} ({data['company_size']}) {data['company_industry']}")
                    print(f"  tenure  : {data['tenure']} (since {data['started']})")
                    print(f"  about   : {data['about'][:260]}")
                    print(f"  exp     : {[e['title'] for e in data['experiences']]}")
                    print()
                else:
                    updates.append((row, json.dumps(data, ensure_ascii=False)))
            if not args.preview and len(updates) >= WRITE_BATCH:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": [
                        {"range": f"'{tab}'!{col_letter(c_out)}{r}", "values": [[v]]}
                        for r, v in updates]}).execute()
                print(f"  -> wrote {len(updates)}", flush=True)
                updates = []
                time.sleep(0.4)

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": [
                {"range": f"'{tab}'!{col_letter(c_out)}{r}", "values": [[v]]}
                for r, v in updates]}).execute()
        print(f"  -> wrote {len(updates)}", flush=True)

    print(f"\nDone - {got}/{len(pending)} profiles returned. "
          f"Blanks are blocked/bad URLs; re-run to retry them.")


if __name__ == "__main__":
    main()
