"""
Enrich missing Company Websites in the HR Leads - Apify Final sheet.

Strategy per company (with name but no website):
  1. Domain guessing — construct slug from company name, try .com/.org/.net/.io/.us
     - 200 response → full content verification (company name words in page)
     - 403/503 response → softer check: domain slug contains company name words
       (handles Cloudflare-protected sites)
  2. Skips — rows missing company name entirely

Found URLs are written back to column L in batches of 10.
Rows where no URL could be verified are left blank for manual lookup.

Run: python3 -W ignore enrich_websites.py
"""

import os
import re
import json
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

BATCH = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Only remove legal entity suffixes — NOT words that are part of brand names
LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|p\.?c\.?|plc\.?|gmbh)\b",
    re.IGNORECASE,
)

TLDS = [".com", ".org", ".net", ".io", ".us", ".co"]
WORKERS = 10  # parallel HTTP workers


def get_google_service():
    with open(TOKEN_PATH) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(token_data, f)
    return build("sheets", "v4", credentials=creds)


# ── Verification helpers ─────────────────────────────────────────────────────

def _name_words(company_name):
    """Meaningful words from the company name (skip short/common words)."""
    noise = {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&", "a",
             "an", "for", "in", "at", "by"}
    words = re.split(r"[\s,.\-&/()]+", company_name.lower())
    return [w for w in words if len(w) > 2 and w not in noise]


def content_matches(html, company_name):
    """Check that the company name's key words appear in the page content."""
    words = _name_words(company_name)
    if not words:
        return True
    content = html[:30000].lower()
    matches = sum(1 for w in words if w in content)
    return matches >= max(1, len(words) // 2)


def domain_matches(url, company_name):
    """
    Softer check for Cloudflare-protected sites (403/503):
    verify the domain itself contains the key company name words.
    """
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    words = _name_words(company_name)
    if not words:
        return True
    matches = sum(1 for w in words if w in domain)
    return matches >= max(1, len(words) // 2)


def verify(url, company_name):
    """
    Fetch URL and confirm it belongs to the company.
    Returns True / False.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if r.status_code == 200:
            return content_matches(r.text, company_name)
        elif r.status_code in (403, 503):
            # Cloudflare / WAF blocked — fall back to domain-name heuristic
            return domain_matches(r.url, company_name)
        # 404, 4xx, 5xx → reject
        return False
    except Exception:
        return False


# ── Domain guessing ──────────────────────────────────────────────────────────

def make_slugs(company_name):
    """Generate candidate domain slugs from a company name."""
    # Strip legal suffixes only
    clean = LEGAL_SUFFIX_RE.sub(" ", company_name).strip()
    # Remove non-alphanumeric except spaces
    clean = re.sub(r"[^a-z0-9\s]", "", clean.lower()).strip()
    words = clean.split()
    if not words:
        return []

    slugs = []
    # Full concatenation: "canopy mortgage" → "canopymortgage"
    slugs.append("".join(words))
    # Hyphenated: "canopy-mortgage"
    if len(words) > 1:
        slugs.append("-".join(words))
    # First two words concatenated (for long names)
    if len(words) >= 3:
        slugs.append("".join(words[:2]))
        slugs.append("-".join(words[:2]))
    # First word alone (for names like "VASION Technologies" → "vasion")
    if len(words) >= 2:
        slugs.append(words[0])

    # Deduplicate while preserving order
    seen = set()
    return [s for s in slugs if s not in seen and not seen.add(s) and len(s) >= 3]


def find_website(company_name):
    """Try domain guessing to find the company's official website."""
    for slug in make_slugs(company_name):
        for tld in TLDS:
            for prefix in ["https://www.", "https://"]:
                url = f"{prefix}{slug}{tld}"
                if verify(url, company_name):
                    return url
    return ""


# ── Google Sheets helpers ────────────────────────────────────────────────────

def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result



# ── Main ─────────────────────────────────────────────────────────────────────

def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def main():
    parser = argparse.ArgumentParser(description="Enrich missing company websites in a Google Sheet")
    parser.add_argument("--sheet_url", default="1jopIsvbAmhxoQmmKXAQTBp1zeujzwOfNAPBsNaCpWqA", help="Google Sheet URL or ID")
    parser.add_argument("--tab", default="Leads", help="Tab name")
    parser.add_argument("--col_company_name", type=int, default=10, help="0-indexed column for company name")
    parser.add_argument("--col_company_url", type=int, default=11, help="0-indexed column for company URL/website")
    args = parser.parse_args()

    SHEET_ID = get_sheet_id_from_url(args.sheet_url)
    TAB = args.tab
    COL_COMPANY_NAME = args.col_company_name
    COL_COMPANY_WEBSITE = args.col_company_url

    print("=== Enrich Missing Company Websites ===\n")
    service = get_google_service()

    print("[1/3] Reading sheet...")
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:AZ"
    ).execute()
    data = result.get("values", [])[1:]
    print(f"  {len(data)} total rows")

    targets = []
    for i, row in enumerate(data):
        company = row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else ""
        website = row[COL_COMPANY_WEBSITE] if len(row) > COL_COMPANY_WEBSITE else ""
        if company.strip() and not website.strip():
            targets.append({"sheet_row": i + 2, "company": company.strip()})

    skipped = sum(
        1 for row in data
        if not (row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else "").strip()
        and not (row[COL_COMPANY_WEBSITE] if len(row) > COL_COMPANY_WEBSITE else "").strip()
    )
    print(f"  {len(targets)} companies need enrichment")
    print(f"  {skipped} rows skipped (no company name)\n")

    if not targets:
        print("Nothing to enrich.")
        return

    print(f"[2/3] Searching for websites ({WORKERS} parallel workers)...")
    updates = []
    found = not_found = 0
    completed = 0

    def flush(svc, upds):
        if not upds:
            return
        data = []
        for u in upds:
            cell = f"'{TAB}'!{col_letter(COL_COMPANY_WEBSITE)}{u['sheet_row']}"
            data.append({"range": cell, "values": [[u["website"]]]})
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
        print(f"\n  → Wrote {len(upds)} websites to sheet")

    def search_one(target):
        return target, find_website(target["company"])

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(search_one, t): t for t in targets}
        for future in as_completed(futures):
            target, website = future.result()
            completed += 1
            status = "✓" if website else "✗"
            print(f"  [{completed}/{len(targets)}] {status}  {target['company'][:50]:50s} → {website or '(not found)'}")

            if website:
                found += 1
                updates.append({"sheet_row": target["sheet_row"], "website": website})
            else:
                not_found += 1

            if len(updates) >= BATCH:
                flush(service, updates)
                updates = []

    flush(service, updates)

    print(f"\n[3/3] Summary")
    print(f"  Found:     {found} / {len(targets)}")
    print(f"  Not found: {not_found}")
    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")


if __name__ == "__main__":
    main()
