"""
Enrich healthcare staffing agency leads from a Sales Navigator export.

Reads:  col A (companyName), col F (LinkedIn numeric company ID)
Writes: col J (linkedin_url) — constructed from ID, zero API cost
        col K (website)      — scraped from LinkedIn company page; Google Search fallback

Resume-safe: skips rows that already have a value in col J.
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
from openai import AzureOpenAI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"
LINKEDIN_ACTOR = "harvestapi~linkedin-company"
GOOGLE_ACTOR = "apify~google-search-scraper"

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")
_azure = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION
) if AZURE_ENDPOINT else None

COL_COMPANY = 0   # A
COL_ID = 5        # F
COL_LINKEDIN = 9   # J
COL_WEBSITE = 10   # K

BATCH = 10

LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|corporation|co)\.?$", re.IGNORECASE
)


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


# ── Apify: LinkedIn company scraper ──────────────────────────────────────────

def scrape_linkedin_companies(id_to_url):
    """
    Scrape LinkedIn company pages via harvestapi~linkedin-companies.
    id_to_url: dict of {company_id: linkedin_url}
    Returns: dict of {company_id: website_domain}
    """
    urls = list(id_to_url.values())
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{LINKEDIN_ACTOR}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN, "timeout": 120},
            json={"companies": list(id_to_url.values())},
            timeout=180,
        )
    except requests.RequestException as e:
        print(f"  [!] LinkedIn actor request failed: {e}")
        return {}

    if resp.status_code not in (200, 201):
        print(f"  [!] LinkedIn actor HTTP {resp.status_code}: {resp.text[:300]}")
        return {}

    try:
        items = resp.json() or []
    except ValueError:
        return {}

    results = {}
    for item in items:
        raw_website = item.get("website") or ""
        # Match back to our row by the numeric company ID in the response
        company_id = str(item.get("id") or "")
        if not company_id:
            company_id = _extract_company_id(item.get("linkedinUrl") or "")
        domain = _bare_domain(raw_website)
        if company_id and domain and not _host_blocked(domain):
            results[company_id] = domain

    return results


def _extract_company_id(url):
    if not url:
        return ""
    m = re.search(r"/company/(\d+)", str(url))
    return m.group(1) if m else ""


def _bare_domain(url):
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlparse(url).netloc.lower()
        host = re.sub(r"^www\.", "", host)
        return host if "." in host else ""
    except Exception:
        return ""


# ── Apify: Google Search fallback ────────────────────────────────────────────

BLOCKED_HOSTS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "simplyhired.com", "careerbuilder.com", "monster.com", "dice.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "yelp.com", "yellowpages.com", "bbb.org", "google.com", "bing.com",
    "crunchbase.com", "zoominfo.com", "apollo.io", "rocketreach.co",
    "wikipedia.org", "trustpilot.com", "bloomberg.com",
    "bamboohr.com", "workday.com", "greenhouse.io", "adp.com", "paychex.com",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
    "calendly.com", "cal.com", "zoom.us", "teams.microsoft.com",
    "hubspot.com", "typeform.com", "mailchimp.com",
}

# Second-level TLDs that are NOT real company domains (e.g. co.uk is a TLD, not a company)
SHARED_SECOND_LEVEL_TLDS = {
    "co.uk", "org.uk", "net.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "co.in", "co.za", "com.br", "co.jp",
}

LLM_SYSTEM = (
    "You identify a staffing or recruitment company's official website from Google search results. "
    "Reply with ONLY the bare domain (e.g. 'example.com') or the word NONE. "
    "Rules: pick the company's own corporate/brand website, not third-party listings. "
    "Reject directories, review sites, job boards, social media, scheduling tools (e.g. calendly.com), "
    "HR/ATS platforms, or any domain that is clearly not the staffing company's own site. "
    "If no candidate is clearly the official site, reply NONE."
)

LLM_VALIDATE_SYSTEM = (
    "You check whether domains are the official websites of staffing or recruitment companies. "
    "You will receive a numbered list of company + domain pairs. "
    "Reply with ONLY the numbers (comma-separated) of domains that are valid company websites. "
    "Reject: scheduling tools (calendly.com, cal.com), job boards, social media, SaaS/HR platforms, "
    "link aggregators (linktr.ee, bio.link), or any domain that is clearly not the company's own website. "
    "If none are valid, reply 'none'."
)


def _llm_validate_websites_batch(items):
    """
    Validate a batch of LinkedIn-scraped domains via LLM.
    items: list of {company, domain}
    Returns: set of domain strings that passed validation.
    Falls back to accepting all on LLM error.
    """
    if not items or _azure is None:
        return {item["domain"] for item in items}

    lines = [f"{i}. Company: {item['company']}  Domain: {item['domain']}"
             for i, item in enumerate(items, 1)]
    try:
        resp = _azure.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=60,
            temperature=0,
            messages=[
                {"role": "system", "content": LLM_VALIDATE_SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ],
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        if answer == "none":
            return set()
        valid = set()
        for part in re.split(r"[,\s]+", answer):
            part = part.strip()
            if part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(items):
                    valid.add(items[idx - 1]["domain"])
        return valid
    except Exception as e:
        print(f"    [!] LLM validate error: {e}")
        return {item["domain"] for item in items}


def apify_google_search(queries, results_per_page=5):
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{GOOGLE_ACTOR}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "queries": "\n".join(queries),
                "resultsPerPage": results_per_page,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "countryCode": "us",
                "includeUnfilteredResults": False,
            },
            timeout=300,
        )
    except requests.RequestException as e:
        print(f"  [!] Google Search request failed: {e}")
        return {}

    if resp.status_code not in (200, 201):
        print(f"  [!] Google Search HTTP {resp.status_code}: {resp.text[:200]}")
        return {}

    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def _extract_host(url):
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", host)
    except Exception:
        return ""


def _registered_domain(host):
    if not host:
        return ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def _host_blocked(host):
    reg = _registered_domain(host)
    return (
        host in BLOCKED_HOSTS or reg in BLOCKED_HOSTS
        or host in SHARED_SECOND_LEVEL_TLDS or reg in SHARED_SECOND_LEVEL_TLDS
    )


def _llm_pick_domain(company_name, candidates):
    if not candidates or _azure is None:
        return ""
    lines = [f"Company: {company_name}", "", "Candidates:"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {c['domain']}")
        lines.append(f"   Title: {c['title'][:150]}")
        lines.append(f"   Snippet: {c['description'][:300]}")
    lines.append("\nReply with ONLY the bare domain or NONE.")
    try:
        resp = _azure.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=60, temperature=0,
            messages=[
                {"role": "system", "content": LLM_SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ],
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        answer = re.sub(r"^https?://", "", answer).split("/")[0].strip(".,'\"`")
        answer = re.sub(r"^www\.", "", answer)
        if not answer or answer == "none" or "." not in answer:
            return ""
        valid = {c["domain"] for c in candidates}
        return answer if answer in valid else ""
    except Exception as e:
        print(f"    [!] LLM error: {e}")
        return ""


def pick_domain_from_search(organic, company_name):
    candidates = []
    seen = set()
    for r in organic[:10]:
        host = _extract_host(r.get("url", ""))
        if not host or _host_blocked(host):
            continue
        reg = _registered_domain(host)
        if reg in seen:
            continue
        seen.add(reg)
        candidates.append({
            "domain": reg,
            "title": r.get("title", "") or "",
            "description": r.get("description", "") or "",
        })
    if not candidates:
        return ""
    return _llm_pick_domain(company_name, candidates)


def google_fallback_batch(company_names, loose=False):
    """
    Run Google Search for a list of company names.
    loose=True: no quotes, broader query — used for fill_blanks pass.
    Returns {company_name: domain}.
    """
    name_to_query = {}
    for name in company_names:
        if loose:
            name_to_query[name] = f"{name} recruitment official website USA"
        else:
            cleaned = LEGAL_SUFFIX_RE.sub("", name.strip()).strip()
            name_to_query[name] = f'"{cleaned}" healthcare staffing official website'

    results = apify_google_search(list(name_to_query.values()), results_per_page=7 if loose else 5)
    query_to_name = {v: k for k, v in name_to_query.items()}

    found = {}
    for q, organic in results.items():
        name = query_to_name.get(q, "")
        if name:
            found[name] = pick_domain_from_search(organic, name) or ""
    return found


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Enrich Sales Navigator agency sheet: LinkedIn URL + website")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    ap.add_argument("--dry_run", action="store_true", help="Preview first 5 rows, no writes")
    ap.add_argument("--fill_blanks", action="store_true",
                    help="Second-pass mode: skip LinkedIn scrape, run loose Google Search on rows missing a website")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    print("=== Enrich Healthcare Agency Leads ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure cols J + K exist (need at least 11 columns)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    col_count = 0
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 11:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS", "length": 11 - col_count,
            }}]}
        ).execute()

    # Write headers if blank
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab_name}'!J1", "values": [["linkedin_url"]]},
            {"range": f"'{tab_name}'!K1", "values": [["website"]]},
        ]},
    ).execute()

    # Read all rows
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:K"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    pending = []
    for i, row in enumerate(data_rows):
        company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        company_id = row[COL_ID].strip() if len(row) > COL_ID else ""
        if not company or not company_id:
            continue
        if args.fill_blanks:
            # Only process rows that already have a LinkedIn URL but no website
            existing_li = row[COL_LINKEDIN].strip() if len(row) > COL_LINKEDIN else ""
            existing_web = row[COL_WEBSITE].strip() if len(row) > COL_WEBSITE else ""
            if not existing_li or existing_web:
                continue
        else:
            existing_li = row[COL_LINKEDIN].strip() if len(row) > COL_LINKEDIN else ""
            if existing_li:
                continue
        pending.append({"row_num": i + 2, "company": company, "id": company_id})

    if args.limit:
        pending = pending[:args.limit]

    mode_label = "fill blanks (loose Google)" if args.fill_blanks else "full enrich"
    print(f"Rows to process [{mode_label}]: {len(pending)}\n")

    if args.dry_run:
        for p in pending[:5]:
            print(f"  Row {p['row_num']}: {p['company']}")
            if args.fill_blanks:
                print(f"    website → [loose Google: {p['company']} recruitment official website USA]")
            else:
                print(f"    linkedin_url → https://www.linkedin.com/company/{p['id']}/")
                print(f"    website      → [LinkedIn scrape → Google fallback]")
        return

    total_linkedin = 0
    total_fallback = 0
    total_not_found = 0

    num_batches = (len(pending) + BATCH - 1) // BATCH
    for b in range(num_batches):
        chunk = pending[b * BATCH:(b + 1) * BATCH]
        print(f"Batch {b + 1}/{num_batches} ({len(chunk)} rows)")

        if args.fill_blanks:
            # Skip LinkedIn scrape — go straight to loose Google Search
            for p in chunk:
                p["linkedin_url"] = f"https://www.linkedin.com/company/{p['id']}/"
                p["website"] = ""
                p["source"] = ""
            fallback = google_fallback_batch([p["company"] for p in chunk], loose=True)
            for p in chunk:
                domain = fallback.get(p["company"], "")
                p["website"] = domain
                p["source"] = "google_loose" if domain else "not_found"
        else:
            # Step 1: construct LinkedIn URLs
            id_to_url = {}
            for p in chunk:
                p["linkedin_url"] = f"https://www.linkedin.com/company/{p['id']}/"
                id_to_url[p["id"]] = p["linkedin_url"]

            # Step 2: scrape LinkedIn company pages
            print(f"  Scraping {len(chunk)} LinkedIn company pages...")
            li_results = scrape_linkedin_companies(id_to_url)

            # Step 2b: LLM-validate LinkedIn-found domains
            li_candidates = [
                {"company": p["company"], "domain": li_results[p["id"]]}
                for p in chunk if li_results.get(p["id"])
            ]
            if li_candidates:
                valid_domains = _llm_validate_websites_batch(li_candidates)
                for p in chunk:
                    domain = li_results.get(p["id"], "")
                    if domain and domain not in valid_domains:
                        print(f"    [LLM rejected] {p['company']} → {domain}")
                        li_results[p["id"]] = ""

            for p in chunk:
                p["website"] = li_results.get(p["id"], "")
                p["source"] = "linkedin" if p["website"] else ""

            # Step 3: Google fallback for rows still missing website
            no_website = [p for p in chunk if not p["website"]]
            if no_website:
                print(f"  {len(no_website)} rows missing website → Google Search fallback...")
                fallback = google_fallback_batch([p["company"] for p in no_website])
                for p in no_website:
                    domain = fallback.get(p["company"], "")
                    p["website"] = domain
                    p["source"] = "google_fallback" if domain else "not_found"

        # Write batch + log
        updates = []
        for p in chunk:
            if p["source"] == "linkedin":
                total_linkedin += 1
            elif p["source"] in ("google_fallback", "google_loose"):
                total_fallback += 1
            else:
                total_not_found += 1
            tag = f"[{p['source']}]" if p["source"] else "[not_found]"
            print(f"  Row {p['row_num']}: {p['company'][:45]:<45} {tag} {p['website']}")
            if not args.fill_blanks:
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(COL_LINKEDIN)}{p['row_num']}",
                    "values": [[p["linkedin_url"]]],
                })
            updates.append({
                "range": f"'{tab_name}'!{col_letter(COL_WEBSITE)}{p['row_num']}",
                "values": [[p["website"]]],
            })

        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
                print(f"  → Wrote {len(updates) // 2} rows to sheet")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Sheet write failed: {e}")

        if b < num_batches - 1:
            time.sleep(1.5)

    print(f"\n=== Done ===")
    print(f"  LinkedIn:  {total_linkedin}")
    print(f"  Fallback:  {total_fallback}")
    print(f"  Not found: {total_not_found}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
