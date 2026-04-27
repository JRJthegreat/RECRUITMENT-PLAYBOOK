"""
Phase 1.9: Find each company's official website domain via Google Search.

Needed before Phase 2 (find_dm.py) because the Indeed scraper's `corporateWebsite`
field actually returns industry strings ("manufacturing", "food and beverage")
for most US companies. Without a real domain in col L, Phase 2 cannot
disambiguate short/generic company names and Phase 3 (enrich_emails) has
nothing to feed Connector OS.

Output: col L (Company Website) populated with bare registered domain
(e.g. "pomwonderful.com"). Skips rows that already have a valid-looking
domain in col L.

Uses Apify Google Search Scraper. Batches 10 queries per call. Dedupes by
unique company name so we only pay per distinct company.

Dry-run default; re-run with --apply to write.
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
from googleapiclient.discovery import build
from anthropic import Anthropic

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
LLM_MODEL = "claude-haiku-4-5-20251001"
_anthropic = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

TAB_NAME = "Leads"
COL_COMPANY = 10    # K
COL_WEBSITE = 11    # L
BATCH = 10

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Hosts we never want as the "company domain"
BLOCKED_HOSTS = {
    # Job boards / aggregators
    "linkedin.com", "indeed.com", "glassdoor.com",
    "ziprecruiter.com", "simplyhired.com", "careerbuilder.com",
    "dice.com", "monster.com", "themuse.com", "builtin.com",
    "wellfound.com", "angellist.com", "jobtoday.com",
    # Social media
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "youtube.com", "pinterest.com",
    # Listings / reviews
    "yelp.com", "yellowpages.com", "bbb.org",
    # Search engines
    "bing.com", "google.com", "duckduckgo.com",
    # Reference
    "wikipedia.org", "trustpilot.com",
    # Business data aggregators
    "crunchbase.com", "zoominfo.com", "apollo.io", "rocketreach.co",
    "contactout.com", "signalhire.com", "opencorporates.com",
    # ATS / HR-tech (often outranks real employers in HR-related searches)
    "bamboohr.com", "workday.com", "greenhouse.io", "lever.co",
    "paychex.com", "gusto.com", "adp.com", "paycom.com", "paylocity.com",
    "shrm.org", "smartrecruiters.com", "icims.com", "ukg.com",
    # Forums / shopping
    "reddit.com", "quora.com", "amazon.com",
}


PUBLIC_SECTOR_TLDS = {"gov", "mil"}


def is_public_sector_host(host):
    """Block any *.gov, *.mil — and the bare TLDs themselves, which are never
    a company site."""
    if host in PUBLIC_SECTOR_TLDS:
        return True
    return host.endswith((".gov", ".mil"))


STOP_TOKENS = {
    "ltd", "limited", "llc", "inc", "corp", "corporation", "co",
    "the", "and", "company", "group", "holdings", "international",
    "services", "consulting", "consultants", "consultancy",
    "partnership", "partners", "solutions", "global",
}


def _name_words(company_name):
    noise = {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&",
             "a", "an", "for", "in", "at", "by",
             "group", "holdings", "limited"}
    words = re.split(r"[\s,.\-&/()+]+", company_name.lower())
    return [w for w in words if len(w) > 2 and w not in noise]


def verify(url, company_name):
    """Fetch URL and confirm ≥half of the company's name-words appear.
    Cloudflare 403/503 falls back to checking the domain name itself."""
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=8, allow_redirects=True)
        if r.status_code == 200:
            content = r.text[:30000].lower()
            words = _name_words(company_name)
            if not words:
                return True
            matches = sum(1 for w in words if w in content)
            return matches >= max(1, len(words) // 2)
        elif r.status_code in (403, 503):
            domain = re.sub(r"^https?://(www\.)?", "", r.url).split("/")[0].lower()
            words = _name_words(company_name)
            if not words:
                return True
            return sum(1 for w in words if w in domain) >= max(1, len(words) // 2)
    except Exception:
        pass
    return False


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


def is_domain_like(s):
    """Does col L already contain something that looks like a domain?"""
    if not s:
        return False
    s = s.strip().lower()
    return bool(re.match(r"^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+$", s)) or "://" in s


def extract_host(url):
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def registered_domain(host):
    """Collapse sub.example.com → example.com. US-flavoured: assumes single-part TLDs."""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def host_is_blocked(host):
    reg = registered_domain(host)
    return (
        host in BLOCKED_HOSTS or reg in BLOCKED_HOSTS
        or is_public_sector_host(host) or is_public_sector_host(reg)
    )


def company_tokens(name):
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in STOP_TOKENS and len(t) >= 2]


def score_host_for_company(host, tokens):
    """Count how many company tokens appear in the host (minus dots).
    Longer/distinctive tokens count extra."""
    host_chars = host.replace(".", "")
    score = 0
    for t in tokens:
        if t in host_chars:
            score += 2 if len(t) >= 5 else 1
    return score


LLM_SYSTEM = (
    "You identify a company's official website from Google search results. "
    "You will receive a company name and numbered candidate results (domain + "
    "title + snippet). Reply with ONLY the bare domain of the official site "
    "(e.g. 'example.com') or the word NONE. "
    "Rules:\n"
    "- Pick the company's own corporate/brand website, not third-party listings.\n"
    "- Reject directories, review sites, job boards, company registries, social media.\n"
    "- An acronym domain is fine IF the snippet clearly names the company.\n"
    "- If the top candidates refer to a different company (name collision), reply NONE.\n"
    "- If no candidate is clearly the official site, reply NONE."
)


def llm_pick_domain(company_name, candidates):
    """Ask Claude Haiku to pick the official website from pre-filtered candidates.
    `candidates` is a list of dicts: {domain, title, description, url}.
    Returns the chosen bare domain, or ''."""
    if not candidates or _anthropic is None:
        return ""

    lines = [f"Company: {company_name}", "", "Candidates:"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {c['domain']}")
        lines.append(f"   Title: {c['title'][:150]}")
        lines.append(f"   Snippet: {c['description'][:300]}")
    lines.append("")
    lines.append("Reply with ONLY the bare domain or NONE.")
    user_msg = "\n".join(lines)

    try:
        resp = _anthropic.messages.create(
            model=LLM_MODEL,
            max_tokens=60,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        answer = resp.content[0].text.strip().lower()
        answer = re.sub(r"^https?://", "", answer)
        answer = answer.split("/")[0].strip().strip(".,'\"`")
        answer = re.sub(r"^www\.", "", answer)
        if not answer or answer == "none" or "." not in answer:
            return ""
        valid = {c["domain"] for c in candidates}
        return answer if answer in valid else ""
    except Exception as e:
        print(f"    [!] LLM error: {e}")
        return ""


def pick_domain_from_results(organic, company_name):
    """Filter blocked hosts, dedupe by registered domain, then ask Claude Haiku
    to evaluate the title + snippet content and pick the official website.
    Returns '' if the LLM declines or no valid candidates remain."""
    candidates = []
    seen = set()
    for r in organic[:10]:
        url = r.get("url", "")
        host = extract_host(url)
        if not host or host_is_blocked(host):
            continue
        reg = registered_domain(host)
        if reg in seen:
            continue
        seen.add(reg)
        candidates.append({
            "url": url,
            "domain": reg,
            "title": r.get("title", "") or "",
            "description": r.get("description", "") or "",
        })

    if not candidates:
        return ""

    return llm_pick_domain(company_name, candidates)


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
    """Strip trailing legal suffixes (Inc/LLC/Corp/…) before searching.
    'Pom Wonderful, Inc.' triggers business registries; 'Pom Wonderful'
    surfaces the actual website."""
    cleaned = LEGAL_SUFFIX_RE.sub("", company.strip()).strip()
    return f'"{cleaned}" official website'


def main():
    ap = argparse.ArgumentParser(description="Find company domains via Google Search → write col L")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="Max unique companies to look up (0 = all)")
    ap.add_argument("--apply", action="store_true", help="Write to sheet. Default: dry run.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite col L even when it already looks like a domain")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Find Company Domains ({mode}) ===\n")

    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB_NAME}!A2:L10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    company_to_rows = {}
    for i, row in enumerate(rows):
        sheet_row = i + 2
        comp = (row[COL_COMPANY] if len(row) > COL_COMPANY else "").strip()
        existing = (row[COL_WEBSITE] if len(row) > COL_WEBSITE else "").strip()
        if not comp:
            continue
        if not args.force and is_domain_like(existing):
            continue
        company_to_rows.setdefault(comp, []).append(sheet_row)

    companies = list(company_to_rows.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"Unique companies needing lookup: {len(companies)}")
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
            domain = pick_domain_from_results(results.get(q, []), comp)
            if domain:
                found[comp] = domain
                print(f"    {comp} → {domain}")
            else:
                not_found.append(comp)
                print(f"    {comp} → NOT FOUND (clearing cell)")
            for r in company_to_rows[comp]:
                updates.append({
                    "range": f"{TAB_NAME}!{col_letter(COL_WEBSITE)}{r}",
                    "values": [[domain]],
                })

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
