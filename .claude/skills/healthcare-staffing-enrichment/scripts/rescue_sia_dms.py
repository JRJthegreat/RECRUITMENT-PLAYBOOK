"""
Rescue pass for SIA rows where AMF found no DM: discover the decision-maker
via Apify Google Search (with the company DOMAIN in the query), then re-run
AMF's PERSON endpoint on the discovered name to get a valid email.

Query shape (domain OR'd into the company clause — strongest disambiguator):
  ("Company" OR "domain.com") ("CEO" OR "Founder" OR "Owner" OR "Managing Director"
   OR "President") site:linkedin.com/in/

Only writes when AMF returns email_status == "valid". Name/title/LinkedIn are
never written without a valid email. Targets = rows with a website but no email.

Schema (both SIA tabs share it):
  A first_name  B last_name  C job_title  D company_name  G company_website
  I linkedin_url  L email

Run:
  python3 -W ignore rescue_sia_dms.py --sheet_url "URL" --tab "TAB" [--preview] [--limit N]
"""

import os
import re
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"
AMF_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"

COL_FIRST, COL_LAST, COL_TITLE, COL_COMPANY = 0, 1, 2, 3
COL_WEBSITE, COL_LINKEDIN, COL_EMAIL = 6, 8, 12  # email L->M after post_url inserted at J

TITLE_WORDS = ("ceo", "chief executive", "founder", "co-founder", "owner",
               "president", "managing director", "managing partner", "principal",
               "partner", "vp", "vice president", "director")

LINKEDIN_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def get_service():
    td = json.load(open(TOKEN_PATH))
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        json.dump(td, open(TOKEN_PATH, "w"))
    return build("sheets", "v4", credentials=creds)


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def extract_domain(url):
    if not url:
        return ""
    d = re.sub(r"^https?://(www\.)?", "", url.strip()).split("/")[0].split("?")[0].lower()
    return d if "." in d else ""


def build_query(company, domain):
    co = f'("{company}" OR "{domain}")' if domain else f'"{company}"'
    return (f'{co} ("CEO" OR "Founder" OR "Owner" OR "Managing Director" '
            f'OR "President") site:linkedin.com/in/')


def apify_google_search(queries):
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={"queries": "\n".join(queries), "resultsPerPage": 5,
              "maxPagesPerQuery": 1, "languageCode": "en", "countryCode": "us",
              "includeUnfilteredResults": False},
        timeout=300,
    )
    if resp.status_code not in (200, 201):
        print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:200]}")
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def parse_result(organic):
    title_field = organic.get("title", "") or ""
    url = organic.get("url", "") or ""
    m = LINKEDIN_RE.search(url)
    if not m or len(m.group(1)) < 3:
        return None
    clean = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title_field, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*[\-–—|·]\s*", clean, maxsplit=2)
    name = parts[0].strip() if parts else ""
    title = ""
    if len(parts) >= 2:
        chunk = parts[1].strip()
        m_at = re.search(r"^(.*?)\s+at\s+(.+)$", chunk, re.IGNORECASE)
        title = (m_at.group(1) if m_at else chunk).strip()
    if not name or " " not in name:
        return None
    return {"name": name, "title": title,
            "linkedin": "https://www.linkedin.com/in/" + m.group(1).split("/")[0]}


def pick_dm(organics):
    """First result whose parsed title looks like a leader; else first parseable."""
    parsed = [p for p in (parse_result(o) for o in organics[:5]) if p]
    for p in parsed:
        if any(w in p["title"].lower() for w in TITLE_WORDS):
            return p
    return parsed[0] if parsed else None


def split_name(full):
    parts = full.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def amf_person(name, domain, company):
    first, last = split_name(name)
    headers = {"Authorization": AMF_API_KEY, "Content-Type": "application/json"}
    body = {"full_name": name, "first_name": first, "last_name": last}
    if domain:
        body["domain"] = domain
    if company:
        body["company_name"] = company
    try:
        resp = requests.post(AMF_PERSON_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        d = resp.json()
        email = d.get("email")
        if email and d.get("email_status") == "valid":
            return email, "valid"
        return None, d.get("email_status", "not_found")
    except requests.exceptions.HTTPError as e:
        return None, f"http_{e.response.status_code}"
    except Exception:
        return None, "error"


def cl(idx):
    return chr(65 + idx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not APIFY_TOKEN or not AMF_API_KEY:
        print("ERROR: APIFY_API_TOKEN / ANYMAILFINDER_API_KEY missing"); return

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:M"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        def c(x):
            return row[x].strip() if len(row) > x else ""
        company, email = c(COL_COMPANY), c(COL_EMAIL)
        domain = extract_domain(c(COL_WEBSITE))
        if company and not email and domain:
            targets.append({"row": i + 2, "company": company, "domain": domain})
    if args.limit:
        targets = targets[:args.limit]

    print(f"=== Google-search DM rescue [{tab}] — {len(targets)} rows ===\n", flush=True)
    for t in targets:
        print(f"  row {t['row']:>3} | {t['company'][:34]:<34} | {build_query(t['company'], t['domain'])}")
    if args.preview:
        print(f"\n[PREVIEW] No API calls. Would search {len(targets)} queries, then AMF each hit.")
        return

    queries = [build_query(t["company"], t["domain"]) for t in targets]
    print("\n--- Apify Google search ---", flush=True)
    results = apify_google_search(queries)

    updates = []
    found = 0
    for t in targets:
        q = build_query(t["company"], t["domain"])
        dm = pick_dm(results.get(q, []))
        if not dm:
            print(f"  -  {t['company'][:32]:<32} → no LinkedIn DM in results", flush=True)
            continue
        email, status = amf_person(dm["name"], t["domain"], t["company"])
        if email:
            found += 1
            first, last = split_name(dm["name"])
            print(f"  +  {t['company'][:30]:<30} → {dm['name']} ({dm['title'][:22]}) | {email}", flush=True)
            updates.append({"row": t["row"], "first": first, "last": last,
                            "title": dm["title"], "linkedin": dm["linkedin"], "email": email})
        else:
            print(f"  ~  {t['company'][:30]:<30} → found {dm['name']} but email [{status}] (not written)", flush=True)

    if updates:
        data = []
        for u in updates:
            data.append({"range": f"'{tab}'!{cl(COL_FIRST)}{u['row']}", "values": [[u["first"]]]})
            data.append({"range": f"'{tab}'!{cl(COL_LAST)}{u['row']}", "values": [[u["last"]]]})
            if u["title"]:
                data.append({"range": f"'{tab}'!{cl(COL_TITLE)}{u['row']}", "values": [[u["title"]]]})
            if u["linkedin"]:
                data.append({"range": f"'{tab}'!{cl(COL_LINKEDIN)}{u['row']}", "values": [[u["linkedin"]]]})
            data.append({"range": f"'{tab}'!{cl(COL_EMAIL)}{u['row']}", "values": [[u["email"]]]})
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
        ).execute()
        time.sleep(1)

    print(f"\n=== Done: {found}/{len(targets)} rescued with valid email ===")


if __name__ == "__main__":
    main()
