"""
Phase 1.95: Enrich missing Company Size (col M) via LinkedIn company-page snippets.

Why this exists: 59% of rows from the Indeed scrape have no Company Size. Phase 2
(find_dm.py) defaults missing-size rows to CEO targeting — but many are 200-500
employee companies that warrant a VP HR contact instead. Filling col M sharpens
DM-tier routing.

Approach: query Apify Google Search with `"{company}" site:linkedin.com/company/`,
regex the standard LinkedIn headcount band ("11-50 employees", "201-500 employees",
…) out of the title/snippet of the top results. No LLM needed — LinkedIn always
formats this consistently.

Resume safety: skips rows where col M already parses via parse_size_lower_bound().

Dry-run default; re-run with --apply to write.
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from pull_dataset import parse_size_lower_bound

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

TAB_NAME = "Leads"
COL_COMPANY = 10    # K
COL_SIZE = 12       # M
BATCH = 10

# LinkedIn company-page format: "11-50 employees", "1,001-5,000 employees", "10,001+ employees".
# Hyphen variants: ASCII '-', en-dash '–', em-dash '—'.
SIZE_PATTERN = re.compile(
    r"\b("
    r"1[\-\u2013\u2014]10|"
    r"11[\-\u2013\u2014]50|"
    r"51[\-\u2013\u2014]200|"
    r"201[\-\u2013\u2014]500|"
    r"501[\-\u2013\u2014]1,?000|"
    r"1,?001[\-\u2013\u2014]5,?000|"
    r"5,?001[\-\u2013\u2014]10,?000|"
    r"10,?001\+|"
    r"10,001\+|"
    r"myself only"
    r")\s*employees\b",
    re.IGNORECASE,
)

STOP_TOKENS = {
    "ltd", "limited", "llc", "inc", "corp", "corporation", "co",
    "the", "and", "company", "group", "holdings", "international",
    "services", "consulting", "consultants", "consultancy",
    "partnership", "partners", "solutions", "global",
}


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
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
    return build("sheets", "v4", credentials=creds)


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def company_tokens(name):
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in STOP_TOKENS and len(t) >= 2]


def linkedin_slug(url):
    """Extract the slug from .../company/<slug>/... → '<slug>' lowercased."""
    try:
        path = urlparse(url).path
        m = re.search(r"/company/([^/?#]+)", path, re.IGNORECASE)
        if not m:
            return ""
        return unquote(m.group(1)).lower()
    except Exception:
        return ""


def slug_matches_company(slug, tokens):
    """Slug must contain ≥ 1 distinctive company token (collapsed alphanumeric)."""
    if not slug or not tokens:
        return False
    slug_chars = re.sub(r"[^a-z0-9]", "", slug)
    return any(t in slug_chars for t in tokens)


def normalize_size(raw):
    """Standardize the matched range to LinkedIn's canonical form."""
    s = raw.strip().lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"\s+", " ", s)
    return s


def extract_size_from_results(organic, company_name):
    """Walk top results; return first headcount band whose LinkedIn slug
    plausibly matches the company. Returns '' if none qualify."""
    tokens = company_tokens(company_name)
    for r in organic[:5]:
        url = r.get("url", "") or ""
        if "linkedin.com/company/" not in url.lower():
            continue
        slug = linkedin_slug(url)
        if not slug_matches_company(slug, tokens):
            continue
        text = " ".join([r.get("title", "") or "", r.get("description", "") or ""])
        m = SIZE_PATTERN.search(text)
        if m:
            return normalize_size(m.group(0))
    return ""


def apify_google_search(queries):
    """Run batched Google search. Returns {query: [organic_results]}."""
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
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
    if resp.status_code not in (200, 201):
        print(f"  [!] Apify HTTP {resp.status_code}: {resp.text[:200]}")
        return {}
    out = {}
    for item in resp.json():
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|corporation|co)\.?$",
    re.IGNORECASE,
)


def build_query(company):
    cleaned = LEGAL_SUFFIX_RE.sub("", company.strip()).strip()
    return f'"{cleaned}" site:linkedin.com/company/'


def main():
    ap = argparse.ArgumentParser(description="Find missing company sizes via LinkedIn snippets → write col M")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Max unique companies to look up (0 = all)")
    ap.add_argument("--apply", action="store_true", help="Write to sheet. Default: dry run.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite col M even when it already parses to a size")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Find Company Sizes ({mode}) ===\n")

    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB_NAME}!A2:M10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    company_to_rows = {}
    for i, row in enumerate(rows):
        sheet_row = i + 2
        comp = (row[COL_COMPANY] if len(row) > COL_COMPANY else "").strip()
        existing = (row[COL_SIZE] if len(row) > COL_SIZE else "").strip()
        if not comp:
            continue
        if not args.force and parse_size_lower_bound(existing) is not None:
            continue
        company_to_rows.setdefault(comp, []).append(sheet_row)

    companies = list(company_to_rows.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies needing size lookup: {len(companies)}")
    if not companies:
        print("Nothing to do.")
        return

    est_credits = len(companies) * 0.007
    print(f"Estimated Apify cost: ~${est_credits:.2f}\n")

    if not args.apply:
        print("Sample of companies we'd look up (first 15):")
        for c in companies[:15]:
            print(f"  {c}")
        print("\n[DRY RUN] No Apify calls. Re-run with --apply.")
        return

    found = {}
    not_found = []
    num_batches = (len(companies) + BATCH - 1) // BATCH

    for b in range(num_batches):
        chunk = companies[b * BATCH:(b + 1) * BATCH]
        queries = [build_query(c) for c in chunk]
        q_to_company = dict(zip(queries, chunk))
        print(f"Batch {b + 1}/{num_batches} ({len(chunk)} companies)")
        results = apify_google_search(queries)

        updates = []
        for q, comp in q_to_company.items():
            size = extract_size_from_results(results.get(q, []), comp)
            if size:
                found[comp] = size
                print(f"    {comp} → {size}")
                for r in company_to_rows[comp]:
                    updates.append({
                        "range": f"{TAB_NAME}!{col_letter(COL_SIZE)}{r}",
                        "values": [[size]],
                    })
            else:
                not_found.append(comp)
                print(f"    {comp} → NOT FOUND")

        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            print(f"  → Wrote {len(updates)} cells")
        time.sleep(1.0)

    print("\n=== Summary ===")
    print(f"Companies looked up: {len(companies)}")
    print(f"  Found:     {len(found)}")
    print(f"  Not found: {len(not_found)}")
    if not_found[:20]:
        print("Sample of not-found companies:")
        for c in not_found[:20]:
            print(f"  {c}")


if __name__ == "__main__":
    main()
