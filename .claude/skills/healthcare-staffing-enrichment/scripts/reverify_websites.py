"""
Re-verify websites in col M using Google (Apify) + Azure OpenAI GPT-4.1.

For each company:
  1. Query: "{company_name} official website {state}" — 5 results
  2. Send all 5 (URL + title + description) to GPT-4.1
  3. GPT-4.1 picks which URL is the real company website (or none)

Outcomes:
  - AI confirms our stored website  → col V = "correct", keep DM
  - AI picks a different website    → col M updated, DM cols cleared, col V = "correct"
  - AI finds nothing useful         → col M cleared, DM cols cleared, col V = "not_correct"

DM cols cleared on wrong: F (dm_name), S (dm_title), T (dm_email), U (dm_linkedin_url)

Run:
  python3 -W ignore reverify_websites.py [--limit N]
"""

import os
import re
import sys
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
from openai import AzureOpenAI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN  = os.getenv("APIFY_API_TOKEN")
APIFY_BASE   = "https://api.apify.com/v2"

AZ_CLIENT = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
)
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"

COL_NAME        = 3   # D
COL_DM_NAME     = 5   # F
COL_STATE       = 10  # K
COL_WEBSITE     = 12  # M
COL_DM_TITLE    = 18  # S
COL_DM_EMAIL    = 19  # T
COL_DM_LINKEDIN = 20  # U
COL_VERIFIED    = 21  # V

APIFY_BATCH = 50
WRITE_BATCH = 10
AI_WORKERS  = 8

SKIP_DOMAINS = {
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "facebook.com", "twitter.com", "instagram.com", "x.com",
    "yelp.com", "bloomberg.com", "crunchbase.com", "zoominfo.com",
    "wikipedia.org", "dnb.com", "bbb.org", "rocketreach.co", "apollo.io",
    "manta.com", "yellowpages.com", "chamberofcommerce.com",
    "opencorporates.com", "buzzfile.com", "dandb.com",
    "highergov.com", "icij.org", "theorg.com", "bebee.com",
    "vivian.com", "nursefly.com", "healthecareers.com", "nursefinders.com",
    "careerbuilder.com", "jobs.com", "zippia.com", "salary.com",
    "google.com", "bing.com", "yahoo.com",
}


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def base_domain(url):
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\d*\.", "", host)
    except Exception:
        return ""


def is_skip(url):
    d = base_domain(url)
    if d.endswith(".gov") or d.endswith(".edu"):
        return True
    return any(s == d or d.endswith("." + s) for s in SKIP_DOMAINS)


def root_url(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


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


def apify_search(queries):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/apify~google-search-scraper/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "queries": "\n".join(queries),
                "resultsPerPage": 5,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "countryCode": "us",
                "includeUnfilteredResults": False,
            },
            timeout=300,
        )
    except requests.exceptions.Timeout:
        print("  [!] Timeout", flush=True)
        return {}
    if resp.status_code not in (200, 201):
        print(f"  [!] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "description": r.get("description", ""),
            }
            for r in item.get("organicResults", []) if r.get("url")
        ]
        if q:
            out[q] = results
    return out


def ai_pick_website(company_name, state, results):
    """Ask GPT-4.1 which result is the company's official website."""
    if not results:
        return ""

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. URL: {r['url']}\n   Title: {r['title']}\n   Description: {r['description']}")

    prompt = f"""You are identifying the official website of a healthcare staffing company.

Company: {company_name}
State: {state}

Google search results:
{chr(10).join(lines)}

Which result URL is this company's OWN official website?
Rules:
- Must be the company's own website, not a directory, job board, LinkedIn, or government site
- The title or description must clearly match this specific company
- If none clearly match, return empty string

Respond with JSON only: {{"website": "full_url_or_empty_string"}}"""

    try:
        resp = AZ_CLIENT.chat.completions.create(
            model=AZ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        url = data.get("website", "").strip()
        if url and not is_skip(url):
            return root_url(url)
    except Exception as e:
        print(f"  [AI error] {e}", flush=True)
    return ""


def flush(service, updates):
    if not updates:
        return
    data = []
    for u in updates:
        data.append({
            "range": f"'{TAB_NAME}'!{col_letter(COL_VERIFIED)}{u['row']}",
            "values": [[u["label"]]],
        })
        data.append({
            "range": f"'{TAB_NAME}'!{col_letter(COL_WEBSITE)}{u['row']}",
            "values": [[u.get("website", "")]],
        })
        if u.get("clear_dm"):
            for col in [COL_DM_NAME, COL_DM_TITLE, COL_DM_EMAIL, COL_DM_LINKEDIN]:
                data.append({
                    "range": f"'{TAB_NAME}'!{col_letter(col)}{u['row']}",
                    "values": [[""]],
                })
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set"); sys.exit(1)

    service = get_service()

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB_NAME}'!{col_letter(COL_VERIFIED)}1",
        valueInputOption="RAW",
        body={"values": [["website_verified"]]},
    ).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name    = row[COL_NAME]    if len(row) > COL_NAME    else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        state   = row[COL_STATE]   if len(row) > COL_STATE   else ""
        if name.strip() and website.strip():
            targets.append({
                "row": i + 2,
                "name": name.strip(),
                "website": website.strip(),
                "state": state.strip(),
            })

    if args.limit:
        targets = targets[:args.limit]

    print(f"=== Re-verifying {len(targets)} websites — Google + GPT-4.1 ===\n", flush=True)

    queries, qmap = [], {}
    for t in targets:
        geo = t["state"] or "USA"
        q = f'{t["name"]} official website {geo}'
        queries.append(q)
        qmap[q] = t

    total_batches = (len(queries) + APIFY_BATCH - 1) // APIFY_BATCH
    confirmed = replaced = cleared = 0
    updates = []

    for b in range(0, len(queries), APIFY_BATCH):
        batch = queries[b:b + APIFY_BATCH]
        bn = b // APIFY_BATCH + 1
        print(f"[Batch {bn}/{total_batches}] Searching Google...", flush=True)

        search_results = apify_search(batch)

        # Run AI picks in parallel for this batch
        def pick(q):
            t = qmap.get(q)
            if not t:
                return q, ""
            results = search_results.get(q, [])
            chosen = ai_pick_website(t["name"], t["state"], results)
            return q, chosen

        with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
            futures = {ex.submit(pick, q): q for q in batch}
            for fut in as_completed(futures):
                q, ai_url = fut.result()
                t = qmap.get(q)
                if not t:
                    continue

                stored_domain = base_domain(t["website"])
                ai_domain     = base_domain(ai_url) if ai_url else ""

                if ai_domain and ai_domain == stored_domain:
                    confirmed += 1
                    updates.append({"row": t["row"], "label": "correct",
                                    "website": t["website"], "clear_dm": False})

                elif ai_domain and ai_domain != stored_domain:
                    replaced += 1
                    print(f"  REPLACED  {t['name'][:48]:48s} → {ai_url[:55]}", flush=True)
                    updates.append({"row": t["row"], "label": "correct",
                                    "website": ai_url, "clear_dm": True})

                else:
                    cleared += 1
                    print(f"  CLEARED   {t['name'][:48]:48s}", flush=True)
                    updates.append({"row": t["row"], "label": "not_correct",
                                    "website": "", "clear_dm": True})

                if len(updates) >= WRITE_BATCH:
                    flush(service, updates)
                    updates = []

        print(f"  Batch {bn} done — confirmed={confirmed} replaced={replaced} cleared={cleared}", flush=True)

    if updates:
        flush(service, updates)

    print(f"\n=== Done ===")
    print(f"  Confirmed correct      : {confirmed}")
    print(f"  Replaced (wrong→right) : {replaced}")
    print(f"  Cleared (no result)    : {cleared}")
    print(f"  Total                  : {len(targets)}")


if __name__ == "__main__":
    main()
