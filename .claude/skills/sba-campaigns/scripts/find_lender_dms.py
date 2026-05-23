"""
Find decision makers for USDA lenders via Google Search + LinkedIn snippets.

Targeting rules by company size (company_size column, LinkedIn-range format):
  1-10, 11-50   → small    → President / CEO / Owner / Founder
  51-200         → community → Chief Credit Officer / CLO / VP Commercial Lending
  201-500,
  501-1,000      → regional  → VP Agricultural/Rural Lending / Director USDA Programs
  1,001-5,000,
  5,001-10,000,
  10,001+        → national  → Director USDA Programs / Head Govt Guaranteed Lending

Non-bank lenders (LLC, Capital, Fund, Finance, Mortgage, BIDCO — no Bank/CU/Savings):
                 → Managing Director / Partner / Principal / Director of Originations

Fallback chain per tier:
  small:     CEO → CLO/EVP
  community: CCO/CLO → CEO
  regional:  VP AgLending → CCO/CLO
  national:  Director USDA → VP AgLending
  non-bank:  MD/Partner → CEO/President

Pass 3 (--pass3): for rows still missing DM after passes 1+2:
  Step A — parse nationwide_contact column (col N) directly as dm_name/dm_title
  Step B — domain-anchored LinkedIn search using website domain (more precise for big banks)

Reads:  col A=lender_name, col B=website, col E=company_size, col N=nationwide_contact
Writes: col AA=dm_name, col AB=dm_title, col AC=dm_linkedin

Skips rows that already have dm_name. Resumable.

Run:
  python3 -W ignore find_lender_dms.py --sheet_url "URL" --tab "TAB_NAME"
  python3 -W ignore find_lender_dms.py --sheet_url "URL" --tab "TAB_NAME" --retry
  python3 -W ignore find_lender_dms.py --sheet_url "URL" --tab "TAB_NAME" --pass3
  python3 -W ignore find_lender_dms.py --sheet_url "URL" --tab "TAB_NAME" --limit 50
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
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

BATCH = 10
APIFY_BATCH_SIZE = 50
SHEET_WRITE_DELAY = 2

COL_NAME = 0
COL_WEBSITE = 1
COL_SIZE = 4
COL_NATIONWIDE = 13
COL_DM_NAME = 26
COL_DM_TITLE = 27
COL_DM_LINKEDIN = 28

# --- Tier detection ---

NON_BANK_SIGNALS = ["llc", "l.l.c.", "capital", "fund", "finance", "lending",
                    "mortgage", "bidco", "inc.", ", inc", "corporation", "authority",
                    "cdfi", "opportunity"]
BANK_SIGNALS = ["bank", "credit union", "savings", "fcu", "fsb", "federal savings",
                "bancorp", "bancshares", "trust company"]


def is_non_bank(name):
    n = name.lower()
    has_bank = any(s in n for s in BANK_SIGNALS)
    has_non_bank = any(s in n for s in NON_BANK_SIGNALS)
    return has_non_bank and not has_bank


def parse_size(size_str):
    """Return upper bound of LinkedIn company_size range as int."""
    if not size_str:
        return None
    s = size_str.replace(",", "").strip()
    if s.endswith("+"):
        return int(re.sub(r"[^\d]", "", s)) + 1
    parts = s.split("-")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


def determine_tier(name, size_str):
    """Return (tier, reasoning) string."""
    if is_non_bank(name):
        return "non_bank", f"Non-bank lender ({size_str or '?'} employees)"
    upper = parse_size(size_str)
    if upper is None or upper <= 50:
        return "small", f"Small lender ({size_str or '?'} employees)"
    if upper <= 200:
        return "community", f"Community bank ({size_str} employees)"
    if upper <= 1000:
        return "regional", f"Regional bank ({size_str} employees)"
    return "national", f"National bank ({size_str} employees)"


def fallback_tier(tier):
    fallbacks = {
        "small": "community",
        "community": "small",
        "regional": "community",
        "national": "regional",
        "non_bank": "small",
    }
    return fallbacks.get(tier, "small")


# --- Search query builders ---

def build_query(company_name, tier):
    q_base = f'"{company_name}"'
    if tier == "small":
        titles = ('"President" OR "CEO" OR "Owner" OR "Founder" OR '
                  '"Managing Director" OR "Principal" OR "Chief Executive"')
    elif tier == "community":
        titles = ('"Chief Credit Officer" OR "Chief Lending Officer" OR "CLO" OR '
                  '"CCO" OR "VP of Commercial Lending" OR "VP Commercial Lending" OR '
                  '"SVP Lending" OR "VP Business Banking" OR "Head of Lending" OR '
                  '"Director of Lending" OR "Executive Vice President"')
    elif tier == "regional":
        titles = ('"VP of Agricultural Lending" OR "VP Agricultural Lending" OR '
                  '"VP of Rural Lending" OR "VP Rural Lending" OR '
                  '"VP Government Guaranteed Lending" OR "VP of Agribusiness" OR '
                  '"Director of USDA Programs" OR "USDA Programs" OR '
                  '"Head of Agricultural Lending" OR "SVP Commercial Banking" OR '
                  '"Chief Lending Officer" OR "Chief Credit Officer"')
    elif tier == "national":
        titles = ('"Director of USDA Programs" OR "Head of Government Guaranteed Lending" OR '
                  '"Head of Govt Guaranteed Lending" OR "SVP Agribusiness" OR '
                  '"Managing Director Rural Finance" OR "National Director Agricultural" OR '
                  '"VP of Agricultural Lending" OR "VP Agricultural Lending" OR '
                  '"Director of Agricultural Banking" OR "Chief Credit Officer"')
    elif tier == "non_bank":
        titles = ('"Managing Director" OR "Partner" OR "Principal" OR '
                  '"Director of Originations" OR "VP of Loan Originations" OR '
                  '"Head of Underwriting" OR "Director of Lending" OR '
                  '"President" OR "CEO" OR "Chief Executive"')
    else:
        titles = '"CEO" OR "President" OR "Owner"'
    return f'{q_base} ({titles}) site:linkedin.com/in/'


# --- LinkedIn result parsing ---

def parse_linkedin_result(result):
    title = result.get("title", "")
    url = result.get("url", "")
    if "linkedin.com/in/" not in url:
        return None, None, None
    title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*[-–]\s*", title, maxsplit=2)
    if len(parts) >= 2:
        person_name = parts[0].strip()
        role = re.sub(r"\s+at\s+.*$", "", parts[1].strip(), flags=re.IGNORECASE).strip()
        return person_name, role, url
    parts = title.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), url
    return None, None, url


# --- Validation keywords per tier ---

VALIDATE = {
    "small": {
        "required": [["ceo", "president", "owner", "founder", "managing director",
                       "principal", "chief executive", "co-founder", "cofounder",
                       "managing member", "managing partner", "executive director"]],
    },
    "community": {
        "required": [
            ["chief credit officer", "cco", "chief lending officer", "clo",
             "vp", "vice president", "svp", "senior vice president",
             "evp", "executive vice president", "head of", "director"],
            ["lending", "loan", "credit", "banking", "commercial",
             "mortgage", "finance", "financial", "business"],
        ],
    },
    "regional": {
        "required": [
            ["vp", "vice president", "svp", "senior vice president",
             "director", "head of", "chief", "evp"],
            ["agri", "rural", "usda", "government guaranteed", "govt guaranteed",
             "agricultural", "agribusiness", "lending", "loan", "credit",
             "commercial", "banking"],
        ],
    },
    "national": {
        "required": [
            ["director", "head", "svp", "senior vice president",
             "managing director", "chief", "vp", "vice president", "national"],
            ["usda", "government guaranteed", "govt guaranteed", "agri",
             "rural", "agricultural", "agribusiness", "lending", "credit",
             "commercial", "banking"],
        ],
    },
    "non_bank": {
        "required": [
            ["managing director", "partner", "principal", "president", "ceo",
             "owner", "founder", "director", "vp", "vice president",
             "chief executive", "chief", "head of"],
        ],
    },
}


def validate_result(name, title, tier):
    if not name or not title:
        return False
    t = title.lower()
    rules = VALIDATE.get(tier, {}).get("required", [])
    for keyword_group in rules:
        if not any(kw in t for kw in keyword_group):
            return False
    return True


# --- Pass 3 helpers ---

def parse_nationwide_contact(nc_str):
    """Parse 'Name, Title' or 'Name' from nationwide_contact field."""
    if not nc_str:
        return None, None
    nc_str = nc_str.strip()
    # Try "Name, Title" split
    parts = nc_str.split(",", 1)
    if len(parts) == 2:
        name = parts[0].strip()
        title = parts[1].strip()
        if name and len(name.split()) >= 2:
            return name, title
    # Single token — could be just a name
    if len(nc_str.split()) >= 2:
        return nc_str, ""
    return None, None


def extract_domain(url):
    """Return bare domain from URL (no www, no path)."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        domain = urlparse(url).netloc.lower()
        domain = re.sub(r"^www\.", "", domain)
        return domain if "." in domain else None
    except Exception:
        return None


def build_domain_query(domain, tier):
    """Domain-anchored LinkedIn search — use domain instead of quoted company name."""
    if tier == "small":
        titles = ('"President" OR "CEO" OR "Owner" OR "Founder" OR '
                  '"Managing Director" OR "Principal" OR "Chief Executive"')
    elif tier == "community":
        titles = ('"Chief Credit Officer" OR "Chief Lending Officer" OR "CLO" OR '
                  '"CCO" OR "VP" OR "Vice President" OR "SVP" OR '
                  '"Head of Lending" OR "Director of Lending" OR "EVP"')
    elif tier == "regional":
        titles = ('"VP" OR "Vice President" OR "SVP" OR "Director" OR "Head" OR "Chief" '
                  'OR "USDA" OR "Agricultural" OR "Rural" OR "Agribusiness" OR "Government Guaranteed"')
    elif tier == "national":
        titles = ('"USDA" OR "Agricultural" OR "Agribusiness" OR "Rural" OR '
                  '"Government Guaranteed" OR "Govt Guaranteed" OR "Director" OR "SVP" OR "Chief"')
    elif tier == "non_bank":
        titles = ('"Managing Director" OR "Partner" OR "Principal" OR '
                  '"Director" OR "President" OR "CEO" OR "Head"')
    else:
        titles = '"CEO" OR "President" OR "Owner"'
    return f'"{domain}" ({titles}) site:linkedin.com/in/'


# --- Google Sheets helpers ---

def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def flush_updates(service, updates, sheet_id, tab):
    if not updates:
        return
    data = []
    for u in updates:
        for col_idx, value in u["cells"].items():
            data.append({
                "range": f"'{tab}'!{col_letter(col_idx)}{u['sheet_row']}",
                "values": [[value]],
            })
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"  -> Wrote {len(updates)} DM records to sheet", flush=True)
    time.sleep(SHEET_WRITE_DELAY)


# --- Apify Google Search ---

def apify_google_search(queries):
    all_results = {}
    total_batches = (len(queries) + APIFY_BATCH_SIZE - 1) // APIFY_BATCH_SIZE

    for batch_start in range(0, len(queries), APIFY_BATCH_SIZE):
        batch = queries[batch_start:batch_start + APIFY_BATCH_SIZE]
        batch_num = batch_start // APIFY_BATCH_SIZE + 1
        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} queries)...", flush=True)

        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json={
                    "queries": "\n".join(batch),
                    "resultsPerPage": 5,
                    "maxPagesPerQuery": 1,
                    "languageCode": "en",
                    "countryCode": "us",
                    "includeUnfilteredResults": False,
                },
                timeout=300,
            )
        except requests.exceptions.Timeout:
            print(f"  Timeout on batch {batch_num}, skipping...", flush=True)
            continue

        if resp.status_code not in (200, 201):
            print(f"  ERROR: HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
            continue

        for item in resp.json():
            query = item.get("searchQuery", {}).get("term", "")
            organic = item.get("organicResults", [])
            if query and organic:
                all_results[query] = organic

        print(f"  Batch {batch_num} done — {len(all_results)} results so far", flush=True)

    return all_results


# --- Pass 3 ---

def run_pass3(args, SHEET_ID, TAB):
    print("=== Find Decision Makers — USDA Lenders [PASS 3: nationwide_contact + domain search] ===\n", flush=True)
    service = get_service()

    print("[1/4] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AE"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"  {len(rows)} total rows", flush=True)

    no_dm = []
    for i, row in enumerate(rows):
        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        dm_name = row[COL_DM_NAME].strip() if len(row) > COL_DM_NAME else ""
        if name and not dm_name:
            no_dm.append({
                "sheet_row": i + 2,
                "company": name,
                "website": row[COL_WEBSITE].strip() if len(row) > COL_WEBSITE else "",
                "size_str": row[COL_SIZE].strip() if len(row) > COL_SIZE else "",
                "nationwide": row[COL_NATIONWIDE].strip() if len(row) > COL_NATIONWIDE else "",
            })

    if args.limit:
        no_dm = no_dm[:args.limit]
    print(f"  {len(no_dm)} lenders still need DM", flush=True)

    if not no_dm:
        print("  Nothing to do!"); return

    # Step A: fill from nationwide_contact
    print(f"\n[2/4] Step A — parsing nationwide_contact ({sum(1 for r in no_dm if r['nationwide'])} rows have it)...", flush=True)
    nc_updates = []
    search_targets = []

    for t in no_dm:
        if t["nationwide"]:
            nc_name, nc_title = parse_nationwide_contact(t["nationwide"])
            if nc_name:
                nc_updates.append({
                    "sheet_row": t["sheet_row"],
                    "cells": {
                        COL_DM_NAME: nc_name,
                        COL_DM_TITLE: nc_title or "",
                        COL_DM_LINKEDIN: "",
                    },
                })
                print(f"  nc {t['company'][:42]:42s} -> {nc_name} ({nc_title})", flush=True)
                continue
        search_targets.append(t)

    if nc_updates:
        for i in range(0, len(nc_updates), BATCH):
            flush_updates(service, nc_updates[i:i + BATCH], SHEET_ID, TAB)
    print(f"  Filled {len(nc_updates)} from nationwide_contact, {len(search_targets)} remain for domain search", flush=True)

    if not search_targets:
        print(f"\nSummary\n  nationwide_contact: {len(nc_updates)}\n  domain search: 0")
        print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
        return

    # Step B: domain-anchored search
    print(f"\n[3/4] Step B — domain-anchored LinkedIn search ({len(search_targets)} queries)...", flush=True)
    query_map = {}
    queries = []
    for t in search_targets:
        domain = extract_domain(t["website"])
        tier, _ = determine_tier(t["company"], t["size_str"])
        if domain:
            q = build_domain_query(domain, tier)
        else:
            q = build_query(t["company"], tier)
        queries.append(q)
        query_map[q] = t

    search_results = apify_google_search(queries)

    print(f"\n[4/4] Matching DMs...", flush=True)
    updates = []
    found = not_found = 0

    for query, target in query_map.items():
        tier, _ = determine_tier(target["company"], target["size_str"])
        organic = search_results.get(query, [])

        best_name = best_title = best_url = None
        for result in organic:
            name, title, url = parse_linkedin_result(result)
            if validate_result(name, title, tier):
                best_name, best_title, best_url = name, title, url
                break

        if best_name:
            found += 1
            updates.append({
                "sheet_row": target["sheet_row"],
                "cells": {
                    COL_DM_NAME: best_name,
                    COL_DM_TITLE: best_title or "",
                    COL_DM_LINKEDIN: best_url or "",
                },
            })
            print(f"  +  {target['company'][:42]:42s} -> {best_name} ({best_title})", flush=True)
        else:
            not_found += 1
            print(f"  x  {target['company'][:42]:42s} -> (not found)", flush=True)

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB)

    print(f"\nSummary")
    print(f"  nationwide_contact filled: {len(nc_updates)}")
    print(f"  domain search found:       {found} / {len(search_targets)}")
    print(f"  still not found:           {not_found}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Find decision makers for USDA lenders")
    parser.add_argument("--sheet_url", required=True, help="Google Sheet URL or ID")
    parser.add_argument("--tab", required=True, help="Tab name")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument("--retry", action="store_true",
                        help="Retry pass: flip to fallback tier for rows still missing DM")
    parser.add_argument("--pass3", action="store_true",
                        help="Pass 3: fill from nationwide_contact, then domain-anchored LinkedIn search")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        return

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab

    if args.pass3:
        run_pass3(args, SHEET_ID, TAB)
        return

    mode = "RETRY (fallback tiers)" if args.retry else "PASS 1"
    print(f"=== Find Decision Makers — USDA Lenders [{mode}] ===\n", flush=True)

    service = get_service()

    print("[1/3] Reading sheet...", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AE"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"  {len(rows)} total rows", flush=True)

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
        size_str = row[COL_SIZE].strip() if len(row) > COL_SIZE else ""
        dm_name = row[COL_DM_NAME].strip() if len(row) > COL_DM_NAME else ""

        if not name:
            continue
        if dm_name:
            continue  # already done

        tier, reasoning = determine_tier(name, size_str)
        if args.retry:
            tier = fallback_tier(tier)
            reasoning = f"RETRY fallback: {reasoning} -> {tier}"

        targets.append({
            "sheet_row": i + 2,
            "company": name,
            "size_str": size_str,
            "tier": tier,
            "reasoning": reasoning,
        })

    if args.limit:
        targets = targets[:args.limit]

    print(f"  {len(targets)} lenders need DM lookup", flush=True)
    if not targets:
        print("  Nothing to do — all lenders already have DMs!")
        return

    by_tier = {}
    for t in targets:
        by_tier[t["tier"]] = by_tier.get(t["tier"], 0) + 1
    for tier, count in sorted(by_tier.items()):
        print(f"    {tier}: {count}", flush=True)

    print(f"\n[2/3] Searching Google ({len(targets)} queries)...", flush=True)
    query_map = {}
    queries = []
    for t in targets:
        q = build_query(t["company"], t["tier"])
        queries.append(q)
        query_map[q] = t

    search_results = apify_google_search(queries)

    print("\n[3/3] Matching DMs...", flush=True)
    updates = []
    found = not_found = 0

    for query, target in query_map.items():
        company = target["company"]
        tier = target["tier"]
        organic = search_results.get(query, [])

        best_name = best_title = best_url = None
        for result in organic:
            name, title, url = parse_linkedin_result(result)
            if validate_result(name, title, tier):
                best_name, best_title, best_url = name, title, url
                break

        if best_name:
            found += 1
            updates.append({
                "sheet_row": target["sheet_row"],
                "cells": {
                    COL_DM_NAME: best_name,
                    COL_DM_TITLE: best_title or "",
                    COL_DM_LINKEDIN: best_url or "",
                },
            })
            print(f"  +  {company[:42]:42s} [{tier:10s}] -> {best_name} ({best_title})", flush=True)
        else:
            not_found += 1
            print(f"  x  {company[:42]:42s} [{tier:10s}] -> (not found)", flush=True)

        if len(updates) >= BATCH:
            flush_updates(service, updates, SHEET_ID, TAB)
            updates = []

    if updates:
        flush_updates(service, updates, SHEET_ID, TAB)

    print(f"\nSummary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    if not_found > 0 and not args.retry:
        print(f"\n  Tip: Run with --retry to try fallback tiers for {not_found} remaining lenders")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()