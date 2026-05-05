"""
Phase 2: Find decision makers via Google Search + LinkedIn.

For each lead without a DM Name:
1. Determine target tier from company size + job seniority + job-title size proxy.
2. Search Google via Apify: '"{company}" "{target titles}" site:linkedin.com/in/'.
3. Parse the LinkedIn snippet (title field + description) into structured
   fields: person name, current title, current company.
4. Validate strictly — title must start with a target keyword, no
   Former/Ex-/Previously markers, company tokens must overlap target,
   URL must be a real /in/<slug> profile, and (when col L is populated)
   any domain mentioned in the snippet must be the company's domain.
5. Write DM Name, DM Title, LinkedIn URL to sheet.

Two passes per row max:
  Pass 1 = primary target from determine_target().
  Pass 2 = next_tier() fallback (one step down/up).
After Pass 2 misses, the row is left empty for Phase 3.5 (AMF rescue).

Tiers:
  ceo         — small co (<50), senior HR hire, or unknown size with thin-HR signal
  hr_manager  — 50-200 employees (small enough that HR Manager is the buyer)
  vp_hr       — 200-500 employees, or unknown size with HR-team signal
"""

import os
import re
import json
import time
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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

BATCH_SIZE = 10
PARALLEL_BATCHES = 6
SHEET_WRITE_DELAY = 0.3

# --- Column indices (0-based, matching HEADERS in pull_dataset.py) ---
COL_JOB_TITLE = 1        # B
COL_COMPANY_NAME = 10    # K
COL_COMPANY_WEBSITE = 11  # L
COL_COMPANY_SIZE = 12    # M
COL_DM_NAME = 19         # T
COL_DM_TITLE = 20        # U
COL_LINKEDIN_URL = 21    # V

# --- Title classification ---

SENIOR_HR_TITLES = [
    "hr director", "director of hr", "director of human resources",
    "vp of people", "vp of hr", "vp human resources", "vp, people",
    "vp, human resources", "vp, hr",
    "head of people", "head of hr", "head of human resources",
    "chief people officer", "chief human resources officer", "chro", "cpo",
    "svp people", "svp hr", "svp, people", "svp, hr",
    "vice president of people", "vice president of hr",
    "vice president, human resources", "vice president, people",
    "director of people", "director of talent",
]

# Roles that imply the company already has a dedicated HR team (so VP HR exists)
HR_TEAM_SIGNAL_TITLES = [
    "recruiter", "talent acquisition", "ta specialist", "ta partner",
    "sourcer", "technical recruiter", "executive recruiter",
    "benefits manager", "benefits specialist",
    "payroll manager", "payroll specialist",
]


def parse_employee_count(count_str):
    if not count_str:
        return None
    s = str(count_str).strip().lower()
    s = s.replace(",", "").replace("+", "")
    s = re.sub(r"\s*employees\s*$", "", s)
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[\u2013\u2014]", "-", s)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    parts = s.split("-")
    try:
        if len(parts) == 2 and parts[1]:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def is_senior_hr(job_title):
    t = (job_title or "").lower()
    return any(kw in t for kw in SENIOR_HR_TITLES)


def is_hr_team_signal(job_title):
    t = (job_title or "").lower()
    return any(kw in t for kw in HR_TEAM_SIGNAL_TITLES)


def determine_target(job_title, employee_count_str):
    """Pick DM tier. Returns (target, reasoning).
    Targets: 'ceo', 'hr_manager', 'vp_hr'."""
    count = parse_employee_count(employee_count_str)

    if is_senior_hr(job_title):
        return "ceo", f"Hiring senior HR leader ({job_title}); target CEO/Founder"

    if count is None:
        return "hr_manager", "Unknown size; target HR first (CEO/Founder via fallback)"

    if count < 50:
        return "ceo", f"Tiny company ({count} emp); target CEO/Founder"

    if count <= 200:
        return "hr_manager", f"Small company ({count} emp); target HR Manager / Head of People"

    return "vp_hr", f"Mid-size company ({count} emp); target VP HR / VP People"


def next_tier(target):
    """Fallback chain: CEO → VP HR → HR Manager → CEO."""
    return {
        "ceo": "vp_hr",
        "vp_hr": "hr_manager",
        "hr_manager": "ceo",
    }.get(target, "ceo")


def _company_clause(company_name, target_domain):
    """Build the company-anchor portion of the query.

    When col L has a real domain, OR it into the company clause so Google
    preferentially ranks LinkedIn profiles that mention the company website
    (often embedded in the experience section). This is the strongest
    disambiguation signal we have for generic names like 'Institutes of Health'.
    """
    if target_domain:
        return f'("{company_name}" OR "{target_domain}")'
    return f'"{company_name}"'


def build_search_query(company_name, target_level, target_domain=""):
    co = _company_clause(company_name, target_domain)
    if target_level == "ceo":
        return (
            f'{co} '
            f'("CEO" OR "Founder" OR "Owner" OR "Managing Director" OR "President") '
            f'site:linkedin.com/in/'
        )
    if target_level == "vp_hr":
        return (
            f'{co} '
            f'("VP" OR "Vice President" OR "Head") '
            f'("HR" OR "Human Resources" OR "People") '
            f'site:linkedin.com/in/'
        )
    if target_level == "hr_manager":
        return (
            f'{co} '
            f'("HR Manager" OR "Human Resources Manager" OR "Head of People" '
            f'OR "People Operations Manager") '
            f'site:linkedin.com/in/'
        )
    return f'{co} "CEO" site:linkedin.com/in/'


# --- Snippet parsing ---

LINKEDIN_PROFILE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def parse_linkedin_result(organic):
    """Pull person_name, current_title, current_company, and snippet text
    from a Google organic LinkedIn result. Returns dict or None."""
    title_field = organic.get("title", "") or ""
    description = organic.get("description", "") or ""
    url = organic.get("url", "") or ""

    m = LINKEDIN_PROFILE_RE.search(url)
    if not m:
        return None
    slug = m.group(1).lower()
    if not slug or len(slug) < 3:
        return None

    # Strip " | LinkedIn" suffix
    title_clean = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title_field, flags=re.IGNORECASE).strip()

    # Common LinkedIn search-result shapes:
    #   "Name - Title - Company"
    #   "Name – Title at Company"
    #   "Name · Title · Company"
    parts = re.split(r"\s*[\-\u2013\u2014|·]\s*", title_clean, maxsplit=2)

    person_name = parts[0].strip() if parts else ""
    current_title = ""
    current_company = ""

    if len(parts) >= 2:
        chunk = parts[1].strip()
        m_at = re.search(r"^(.*?)\s+at\s+(.+)$", chunk, re.IGNORECASE)
        if m_at:
            current_title = m_at.group(1).strip()
            current_company = m_at.group(2).strip()
        else:
            current_title = chunk

    if len(parts) >= 3 and not current_company:
        current_company = parts[2].strip()

    return {
        "name": person_name,
        "title": current_title,
        "company": current_company,
        "snippet": description,
        "url": url,
        "slug": slug,
    }


# --- Validation ---

NOISE_WORDS = {
    "inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&",
    "a", "an", "for", "in", "at", "by", "group", "services",
    "company", "holdings", "international", "global",
}

FORMER_PATTERNS = re.compile(
    r"\b(former|formerly|ex[\-\s]|previously|past)\b",
    re.IGNORECASE,
)


def name_words(text):
    words = re.split(r"[\s,.\-&/()+]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in NOISE_WORDS]


def title_matches_target(title_text, target_level):
    """Title must START with a target keyword (or have it as the leading noun)
    so 'Reports to CEO' / 'Assistant to VP HR' get rejected."""
    t = (title_text or "").lower().strip()
    if not t:
        return False

    if target_level == "ceo":
        starts = (
            "ceo", "chief executive", "founder", "co-founder", "cofounder",
            "co founder", "owner", "managing director", "president",
        )
        return t.startswith(starts)

    if target_level == "vp_hr":
        # Must start with VP/Vice President/Head AND mention HR/People
        starts_vp = t.startswith(("vp", "vice president", "head of", "head ,", "head, "))
        contains_hr = any(kw in t for kw in (
            "hr", "human resources", "people", "talent",
        ))
        return starts_vp and contains_hr

    if target_level == "hr_manager":
        starts = (
            "hr manager", "human resources manager", "human resource manager",
            "head of people", "head of hr", "head of human resources",
            "people operations manager", "people ops manager",
            "people operations lead", "manager, human resources",
            "manager, people", "manager of people",
        )
        return t.startswith(starts)

    return False


def company_overlap(snippet_company, target_company):
    """Require every distinctive (≥5-char) target token to appear in the
    snippet company. Short tokens collide too easily ('Wing Inflatables' vs
    'Wing Group') so they aren't enough on their own."""
    a = set(name_words(snippet_company))
    b = set(name_words(target_company))
    if not b:
        return 1.0
    long_b = {t for t in b if len(t) >= 5}
    if long_b:
        return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a & b else 0.0


def snippet_company_overlap(full_snippet, target_company):
    """Same long-token rule, applied to the description blob when the title
    field didn't include a parsed company."""
    a = set(re.split(r"[\s,.\-&/()+]+", (full_snippet or "").lower()))
    b = set(name_words(target_company))
    if not b:
        return 1.0
    long_b = {t for t in b if len(t) >= 5}
    if long_b:
        return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a & b else 0.0


def domain_in_snippet(snippet, target_domain):
    """Domain literal OR its main label appears in the snippet text.
    Used when target_domain is populated AND company name is generic."""
    if not target_domain or not snippet:
        return False
    s = snippet.lower()
    if target_domain.lower() in s:
        return True
    main = target_domain.split(".")[0].lower()
    if len(main) >= 5 and main in re.sub(r"[^a-z0-9]", "", s):
        return True
    return False


def domain_anchor_ok(snippet, target_domain):
    """If the snippet mentions a non-LinkedIn web domain, it should match
    the target's domain. Returns True when there's nothing to disprove."""
    if not target_domain:
        return True
    target_main = target_domain.split(".")[0].lower()
    if len(target_main) < 4:
        return True  # too generic to anchor on (e.g. 'abc.com')
    other = re.findall(r"\b([a-z0-9][a-z0-9\-]+\.[a-z]{2,})\b", (snippet or "").lower())
    other_mains = {d.split(".")[0] for d in other}
    other_mains.discard("linkedin")
    other_mains.discard("lnkd")
    if not other_mains:
        return True
    return target_main in other_mains


# Common dictionary words that, even ≥5 chars, don't disambiguate a company
# from another org sharing them ('Institutes of Health' collides with NIH etc.).
GENERIC_COMPANY_TOKENS = {
    "institute", "institutes", "health", "healthcare", "medical", "clinic",
    "hospital", "hospitals", "center", "centers", "centre", "centres",
    "services", "service", "solutions", "systems", "global", "international",
    "national", "american", "consulting", "consultants", "associates",
    "associated", "industries", "industry", "technologies", "technology",
    "products", "management", "enterprise", "enterprises", "professional",
    "school", "schools", "academy", "university", "college", "education",
    "research", "foundation", "foundations", "network", "partners",
    "community", "regional", "general", "advanced", "premier", "select",
    "united", "first", "family",
}


def has_distinctive_token(target_company):
    """True iff the target name has at least one distinctive token —
    a ≥5-char token that is NOT in GENERIC_COMPANY_TOKENS."""
    for w in name_words(target_company):
        if len(w) >= 5 and w not in GENERIC_COMPANY_TOKENS:
            return True
    return False


def validate_result(parsed, target_company, target_level, target_domain=""):
    """All-or-nothing validation. Returns True iff every check passes."""
    if not parsed:
        return False
    if not parsed["name"] or not parsed["title"]:
        return False

    # Reject ex-employees / past roles
    if FORMER_PATTERNS.search(parsed["title"]):
        return False

    # Title strictness
    if not title_matches_target(parsed["title"], target_level):
        return False

    # Company match: prefer parsed company. Fall back to description blob
    # only when target has distinctive (≥5-char) tokens — short single-token
    # company names ('Yami', 'Steer') collide with first names and common
    # words in the description, so we refuse to guess.
    if parsed["company"]:
        if company_overlap(parsed["company"], target_company) < 1.0:
            return False
    else:
        target_long = {t for t in name_words(target_company) if len(t) >= 5}
        if not target_long:
            return False
        if snippet_company_overlap(parsed["snippet"], target_company) < 1.0:
            return False

    # Domain anchor (only when col L populated)
    if not domain_anchor_ok(parsed["snippet"], target_domain):
        return False

    # Generic-name disambiguation: if target is something like 'Institutes
    # of Health' (every distinctive token is a generic English word), the
    # token-overlap check above can't tell our company apart from National
    # Institutes of Health, etc. Require the actual domain to appear in the
    # snippet text in that case. The query already OR's the domain in, so
    # legitimate matches usually surface a profile that mentions it.
    if target_domain and not has_distinctive_token(target_company):
        if not domain_in_snippet(parsed["snippet"], target_domain):
            return False

    return True


# --- Google Sheets ---

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


# --- Apify ---

def apify_google_search(queries):
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
        print(f"  ERROR from Apify: HTTP {resp.status_code}: {resp.text[:300]}")
        return {}
    items = resp.json()
    out = {}
    for item in items:
        q = item.get("searchQuery", {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


# --- Pass orchestration ---

def process_batch(leads, target_map, dry_run):
    """Pure search step. Returns (updates, found_count, not_found_count, log_lines)."""
    queries = []
    query_to_lead = {}
    for lead in leads:
        target = target_map[lead["sheet_row"]]
        q = build_search_query(lead["company_name"], target, lead.get("domain", ""))
        queries.append(q)
        query_to_lead[q] = lead

    if dry_run:
        lines = [f"  [DRY] Row {lead['sheet_row']}: {lead['company_name']} → {target_map[lead['sheet_row']]}"
                 for lead in leads]
        return [], 0, len(leads), lines

    search_results = apify_google_search(queries)

    updates = []
    log_lines = []
    found = 0
    not_found = 0

    for q, lead in query_to_lead.items():
        target = target_map[lead["sheet_row"]]
        organic = search_results.get(q, [])

        matched = None
        for r in organic[:5]:
            parsed = parse_linkedin_result(r)
            if not parsed:
                continue
            if validate_result(parsed, lead["company_name"], target, lead.get("domain", "")):
                matched = parsed
                break

        if matched:
            updates.append({
                "sheet_row": lead["sheet_row"],
                "person_name": matched["name"],
                "result_title": matched["title"],
                "linkedin_url": matched["url"],
            })
            log_lines.append(f"    Row {lead['sheet_row']}: {lead['company_name']} → "
                             f"{matched['name']} ({matched['title']}) [{target}]")
            found += 1
        else:
            log_lines.append(f"    Row {lead['sheet_row']}: {lead['company_name']} → NOT FOUND [{target}]")
            not_found += 1

    return updates, found, not_found, log_lines


def write_updates(service, sheet_id, tab_name, updates):
    if not updates:
        return
    data = []
    for u in updates:
        row = u["sheet_row"]
        data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_NAME)}{row}",
                     "values": [[u["person_name"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_DM_TITLE)}{row}",
                     "values": [[u["result_title"]]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_LINKEDIN_URL)}{row}",
                     "values": [[u["linkedin_url"]]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def collect_leads(rows, target_fn, limit):
    leads = []
    target_map = {}
    for i, row in enumerate(rows):
        if limit > 0 and len(leads) >= limit:
            break
        if cell(row, COL_DM_NAME):
            continue
        company_name = cell(row, COL_COMPANY_NAME)
        if not company_name:
            continue
        job_title = cell(row, COL_JOB_TITLE)
        company_size = cell(row, COL_COMPANY_SIZE)
        domain = cell(row, COL_COMPANY_WEBSITE)
        target, _ = target_fn(job_title, company_size)
        sheet_row = i + 2
        leads.append({
            "sheet_row": sheet_row,
            "company_name": company_name,
            "job_title": job_title,
            "domain": domain if "." in domain else "",
        })
        target_map[sheet_row] = target
    return leads, target_map


def run_pass(service, sheet_id, tab_name, rows, label, target_fn, limit, dry_run):
    leads, target_map = collect_leads(rows, target_fn, limit)
    print(f"\n{label}: {len(leads)} leads")
    if not leads:
        return 0, 0

    found_total = 0
    not_found_total = 0
    num_batches = (len(leads) + BATCH_SIZE - 1) // BATCH_SIZE
    batches = [leads[b * BATCH_SIZE:(b + 1) * BATCH_SIZE] for b in range(num_batches)]

    with ThreadPoolExecutor(max_workers=PARALLEL_BATCHES) as pool:
        futs = {pool.submit(process_batch, batch, target_map, dry_run): i
                for i, batch in enumerate(batches)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                updates, found, not_found, log_lines = fut.result()
            except Exception as e:
                print(f"  Batch {idx + 1}/{num_batches} CRASHED: {e}")
                continue
            print(f"  Batch {idx + 1}/{num_batches} ({found} found, {not_found} miss)")
            for line in log_lines:
                print(line)
            found_total += found
            not_found_total += not_found
            if updates and not dry_run:
                write_updates(service, sheet_id, tab_name, updates)
                time.sleep(SHEET_WRITE_DELAY)
    return found_total, not_found_total


def main():
    ap = argparse.ArgumentParser(description="Find decision makers via Google Search + LinkedIn")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    print("=== Find Decision Makers (Google + LinkedIn) ===\n")
    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_name = meta["sheets"][0]["properties"]["title"]
    print(f"Tab: '{tab_name}'")

    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        print("No data rows.")
        return
    data_rows = all_rows[1:]

    f1, nf1 = run_pass(
        service, sheet_id, tab_name, data_rows,
        "Pass 1 — primary tier", determine_target,
        limit=args.limit, dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"\n[DRY RUN] Would search {f1 + nf1} leads")
        return

    print(f"\nPass 1: {f1} found, {nf1} missed")

    if nf1 > 0:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
        ).execute()
        data_rows = result.get("values", [])[1:]

        def fallback_target(job_title, company_size):
            t, _ = determine_target(job_title, company_size)
            nt = next_tier(t)
            return nt, f"Fallback: {t} → {nt}"

        f2, nf2 = run_pass(
            service, sheet_id, tab_name, data_rows,
            "Pass 2 — next-tier fallback", fallback_target,
            limit=args.limit, dry_run=args.dry_run,
        )
        print(f"\nPass 2: {f2} found, {nf2} still missed")
        print(f"\nTotal: {f1 + f2} found, {nf2} unfound (Phase 3.5 AMF rescue)")

    print(f"\nSheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
