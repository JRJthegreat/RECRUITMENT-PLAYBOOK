"""
Find the decision maker (CEO/Owner or COO) for each ICP company on the
Multiple Openings / Single Opening tabs, per the Target Role in column Z.

Routing (read from col Z, set by assign_target_role.py):
  "COO"      -> Pass 1: COO / Chief Operating Officer / VP/Dir of Operations
                Pass 2 fallback: CEO / Owner / Founder / President
  "CEO/Owner"-> Pass 1: CEO / Owner / Founder / President / Managing Partner
                Pass 2 fallback: Managing Director / Principal

Domain is included in the search query as ("Company" OR "domain.com") — same
pattern as the working hr-leads-indeed/find_dm.py — so Google can match the
right person even when the LinkedIn snippet uses the domain rather than the
exact company name.

Writes per company (all its rows): AA DM Name, AB DM Title, AC DM LinkedIn URL.
Resume-safe (skips companies with AA already set). Batch-of-10 with sheet writes.

Usage:
  python3 -W ignore find_dm_openings.py --sheet_url "URL" [--dry_run]
                                         [--tabs "Multiple Openings,Single Opening"]
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

ANYMAILFINDER_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

BATCH_SIZE = 10

# Opening-tab columns (0-based)
COL_COMPANY_NAME = 12  # M
COL_WEBSITE      = 20  # U
COL_TARGET       = 25  # Z
COL_DM_NAME      = 26  # AA
COL_DM_TITLE     = 27  # AB
COL_DM_LINKEDIN  = 28  # AC
COL_DM_EMAIL     = 29  # AD  — written by AMF pass only

DEFAULT_TABS = ["Multiple Openings", "Single Opening"]

# Query title strings and validation tuples per pass
CEO_QUERY   = ('"CEO" OR "Chief Executive Officer" OR "President" OR "Owner" OR "Co-Owner" '
               'OR "Founder" OR "Co-Founder" OR "Managing Partner"')
CEO_STARTS  = ("ceo", "chief executive officer", "president", "owner", "co-owner",
               "founder", "co-founder", "managing partner")

COO_QUERY   = ('"COO" OR "Chief Operating Officer" OR "VP of Operations" '
               'OR "Vice President of Operations" OR "Director of Operations"')
COO_STARTS  = ("coo", "chief operating officer", "vp of operations",
               "vice president of operations", "vp operations",
               "director of operations", "head of operations")

# Fallback when Pass 1 misses
FALLBACK_QUERY  = ('"Managing Director" OR "Principal" OR "Founder" OR "Owner" OR "President"')
FALLBACK_STARTS = ("managing director", "principal", "founder", "owner", "president")


# ── Query building ──────────────────────────────────────────────────────────

def _company_clause(company_name, target_domain):
    """Include domain as an OR alternative so Google can anchor on either.
    Mirrors the working pattern in hr-leads-indeed/find_dm.py."""
    if target_domain:
        return f'("{company_name}" OR "{target_domain}")'
    return f'"{company_name}"'


def build_query_preview(company_name, pass_num, target_role, target_domain=""):
    q_titles, _ = pass_config(pass_num, target_role)
    return f"{_company_clause(company_name, target_domain)} ({q_titles}) site:linkedin.com/in/"


# ── Snippet parsing ─────────────────────────────────────────────────────────

LINKEDIN_PROFILE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def parse_linkedin_result(organic):
    title_field = organic.get("title", "") or ""
    description = organic.get("description", "") or ""
    url = organic.get("url", "") or ""
    m = LINKEDIN_PROFILE_RE.search(url)
    if not m:
        return None
    slug = m.group(1).lower()
    if not slug or len(slug) < 3:
        return None
    title_clean = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title_field, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*[\-–—|·]\s*", title_clean, maxsplit=2)
    person_name = parts[0].strip() if parts else ""
    current_title = ""
    current_company = ""
    if len(parts) >= 2:
        chunk = parts[1].strip()
        m_at = re.search(r"^(.*?)\s+at\s+(.+)$", chunk, re.IGNORECASE)
        if m_at:
            current_title, current_company = m_at.group(1).strip(), m_at.group(2).strip()
        else:
            current_title = chunk
    if len(parts) >= 3 and not current_company:
        current_company = parts[2].strip()
    return {"name": person_name, "title": current_title, "company": current_company,
            "snippet": description, "url": url, "slug": slug}


# ── Validation ──────────────────────────────────────────────────────────────

NOISE_WORDS = {
    "inc", "llc", "ltd", "corp", "co", "the", "of", "and", "&", "a", "an",
    "for", "in", "at", "by", "group", "services", "company", "holdings",
    "international", "global", "pllc", "pc", "pa",
}
FORMER_RE = re.compile(r"\b(former|formerly|ex[\-\s]|previously|past)\b", re.IGNORECASE)
GENERIC_TOKENS = {
    "health", "healthcare", "medical", "clinic", "center", "care", "primary",
    "family", "wellness", "institute", "services", "service", "solutions",
    "consulting", "associates", "community", "regional", "advanced", "network",
    "partners", "management", "professional", "national", "general", "group",
}


def name_words(text):
    words = re.split(r"[\s,.\-&/()+]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in NOISE_WORDS]


def title_matches(title_text, starts_tuple):
    """Accept titles that START with a keyword (strict) OR CONTAIN one as a standalone
    word/phrase — handles multi-title strings like 'President CEO Owner at ...'."""
    t = (title_text or "").lower().strip()
    if not t:
        return False
    if t.startswith(starts_tuple):
        return True
    # loose pass: keyword appears as a word boundary anywhere in the title
    for kw in starts_tuple:
        pattern = r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"
        if re.search(pattern, t):
            return True
    return False


def company_overlap(snippet_company, target_company):
    a = set(name_words(snippet_company))
    b = set(name_words(target_company))
    if not b:
        return 1.0
    long_b = {w for w in b if len(w) >= 5}
    if long_b:
        return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a & b else 0.0


def snippet_company_overlap(snippet, target_company):
    a = set(re.split(r"[\s,.\-&/()+]+", (snippet or "").lower()))
    b = set(name_words(target_company))
    if not b:
        return 1.0
    long_b = {w for w in b if len(w) >= 5}
    if long_b:
        return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a & b else 0.0


def domain_in_snippet(snippet, domain):
    if not domain or not snippet:
        return False
    s = snippet.lower()
    if domain.lower() in s:
        return True
    main = domain.split(".")[0].lower()
    return len(main) >= 5 and main in re.sub(r"[^a-z0-9]", "", s)


def domain_anchor_ok(snippet, domain):
    if not domain:
        return True
    target_main = domain.split(".")[0].lower()
    if len(target_main) < 4:
        return True
    others = re.findall(r"\b([a-z0-9][a-z0-9\-]+\.[a-z]{2,})\b", (snippet or "").lower())
    other_mains = {d.split(".")[0] for d in others} - {"linkedin", "lnkd"}
    return not other_mains or target_main in other_mains


def has_distinctive_token(target_company):
    return any(len(w) >= 5 and w not in GENERIC_TOKENS for w in name_words(target_company))


def validate_result(parsed, target_company, starts_tuple, domain=""):
    if not parsed or not parsed["name"] or not parsed["title"]:
        return False
    if FORMER_RE.search(parsed["title"]):
        return False
    if not title_matches(parsed["title"], starts_tuple):
        return False
    co = parsed["company"]
    # treat ellipsis / short junk as empty — fall through to snippet check
    co_clean = co.strip(".… \t") if co else ""
    if co_clean and len(co_clean) >= 3:
        if company_overlap(co_clean, target_company) < 1.0:
            return False
    else:
        target_long = {w for w in name_words(target_company) if len(w) >= 5}
        if not target_long or snippet_company_overlap(parsed["snippet"], target_company) < 1.0:
            return False
    if not domain_anchor_ok(parsed["snippet"], domain):
        return False
    if domain and not has_distinctive_token(target_company):
        if not domain_in_snippet(parsed["snippet"], domain):
            return False
    return True


# ── Sheets / Apify ──────────────────────────────────────────────────────────

def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"], token_uri=td["token_uri"],
        client_id=td["client_id"], client_secret=td["client_secret"],
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


def domain_from_website(website):
    w = (website or "").strip().lower()
    if not w:
        return ""
    if "://" not in w:
        w = "http://" + w
    net = urlparse(w).netloc
    return net[4:] if net.startswith("www.") else net


def tab_exists(service, sheet_id, title):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def ensure_columns(service, sheet_id, title, min_cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"appendDimension": {
                        "sheetId": gid, "dimension": "COLUMNS", "length": min_cols - have}}]},
                ).execute()
            return


def apify_google_search(queries):
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={"queries": "\n".join(queries), "resultsPerPage": 5, "maxPagesPerQuery": 1,
              "languageCode": "en", "countryCode": "us", "includeUnfilteredResults": False},
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


def pass_config(pass_num, target_role):
    """Return (query_titles, starts_tuple) for this pass/role combination.
    Pass 1: primary target. Pass 2: fallback.
      COO  P1 -> COO titles; P2 -> CEO/Owner (small clinics rarely have a COO)
      CEO  P1 -> CEO/Owner;  P2 -> broader founder/managing-director sweep
    """
    if pass_num == 1:
        if target_role == "COO":
            return COO_QUERY, COO_STARTS
        return CEO_QUERY, CEO_STARTS
    else:
        if target_role == "COO":
            # no COO found -> try the owner/founder/president
            return CEO_QUERY, CEO_STARTS
        return FALLBACK_QUERY, FALLBACK_STARTS


def search_pass(companies, pass_num, results_cache):
    """Build queries for a pass, call Apify, return {company_name: parsed_hit or None}."""
    to_search = {g["name"]: g for g in companies}
    queries = []
    q_to_name = {}
    for name, g in to_search.items():
        q_titles, _ = pass_config(pass_num, g["target"])
        q = f'{_company_clause(name, g["domain"])} ({q_titles}) site:linkedin.com/in/'
        queries.append(q)
        q_to_name[q] = name

    raw = apify_google_search(queries)
    results_cache.update(raw)

    hits = {}
    for q, name in q_to_name.items():
        g = to_search[name]
        _, starts = pass_config(pass_num, g["target"])
        organics = raw.get(q, [])
        for org in organics:
            parsed = parse_linkedin_result(org)
            if validate_result(parsed, name, starts, g["domain"]):
                hits[name] = parsed
                break
        else:
            hits[name] = None
    return hits


# ── AnyMail Finder ─────────────────────────────────────────────────────────

def _amf_headers():
    return {"Authorization": ANYMAILFINDER_API_KEY, "Content-Type": "application/json"}


def amf_find_person_email(name, domain):
    """Find email for a known person. Returns {email, status}."""
    if not name or not domain:
        return {"email": None, "status": "missing_data"}
    try:
        resp = requests.post(
            "https://api.anymailfinder.com/v5.1/find-email/person",
            headers=_amf_headers(),
            json={"full_name": name, "domain": domain},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        email = data.get("email")
        status = data.get("email_status", "unknown")
        return {"email": email if email and status == "valid" else None,
                "status": status or "not_found"}
    except Exception as e:
        return {"email": None, "status": f"error: {type(e).__name__}"}


def amf_find_dm(domain, company_name, target_role):
    """Find DM + email via /decision-maker for companies where Google found nothing.
    COO targets try 'coo' first, fall back to 'ceo'. CEO/Owner targets go straight to 'ceo'."""
    categories = ["coo", "ceo"] if target_role == "COO" else ["ceo"]
    for cat in categories:
        try:
            body = {"decision_maker_category": [cat]}
            if domain:
                body["domain"] = domain
            if company_name:
                body["company_name"] = company_name
            resp = requests.post(AMF_DM_URL, headers=_amf_headers(), json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            name = data.get("person_full_name", "") or ""
            if name:
                email = data.get("valid_email") or data.get("email")
                status = data.get("email_status", "unknown")
                return {
                    "name": name,
                    "title": data.get("person_job_title", "") or "",
                    "linkedin": data.get("person_linkedin_url", "") or "",
                    "email": email if email and status == "valid" else None,
                    "status": status,
                    "category": cat,
                }
        except Exception:
            pass
    return None


def write_updates(service, sheet_id, updates):
    if not updates:
        return
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": updates}
            ).execute()
            return
        except Exception as e:
            if attempt < 3:
                time.sleep(4)
            else:
                print(f"  [!] sheet write failed: {e}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Find CEO/Owner or COO per ICP company")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tabs", default=",".join(DEFAULT_TABS))
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]

    print(f"=== Find DM (CEO/Owner or COO) {'[DRY RUN]' if args.dry_run else ''} ===\n")

    # Collect all companies that still need a DM, keyed by name (dedup across tabs)
    all_companies = []
    tab_of = {}  # company_name -> tab title

    for tab in tabs:
        if not tab_exists(service, sheet_id, tab):
            print(f"[!] tab {tab!r} not found, skipping")
            continue
        ensure_columns(service, sheet_id, tab, COL_DM_EMAIL + 1)
        # write headers
        if not args.dry_run:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{tab}'!{col_letter(COL_DM_NAME)}1",    "values": [["DM Name"]]},
                    {"range": f"'{tab}'!{col_letter(COL_DM_TITLE)}1",   "values": [["DM Title"]]},
                    {"range": f"'{tab}'!{col_letter(COL_DM_LINKEDIN)}1","values": [["DM LinkedIn URL"]]},
                    {"range": f"'{tab}'!{col_letter(COL_DM_EMAIL)}1",   "values": [["Email"]]},
                ]}).execute()

        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:AD10000"
        ).execute().get("values", [])

        groups = {}
        for i, r in enumerate(rows):
            name = cell(r, COL_COMPANY_NAME)
            if not name:
                continue
            if name not in groups:
                groups[name] = {
                    "tab": tab, "name": name,
                    "target": cell(r, COL_TARGET) or "CEO/Owner",
                    "domain": domain_from_website(cell(r, COL_WEBSITE)),
                    "row_nums": [], "has_dm": False,
                }
            groups[name]["row_nums"].append(i + 2)
            if cell(r, COL_DM_NAME):
                groups[name]["has_dm"] = True
            if not groups[name]["domain"]:
                groups[name]["domain"] = domain_from_website(cell(r, COL_WEBSITE))

        for name, g in groups.items():
            if not g["has_dm"] and name not in tab_of:
                all_companies.append(g)
                tab_of[name] = tab

    print(f"Companies needing a DM: {len(all_companies)}")
    if args.dry_run:
        for g in all_companies[:15]:
            print(f"  [{g['target']:9s}] {g['name'][:42]:42s}  domain={g['domain']}")
            print(f"    Pass1 q: {build_query_preview(g['name'], 1, g['target'], g['domain'])[:80]}")
        print("\n[DRY RUN] No Apify calls.")
        return

    # Process in batches of 10, two passes per batch
    found_total = miss_total = 0
    for start in range(0, len(all_companies), BATCH_SIZE):
        batch = all_companies[start:start + BATCH_SIZE]
        raw_cache = {}

        # Pass 1
        hits1 = search_pass(batch, 1, raw_cache)
        need_pass2 = [g for g in batch if not hits1.get(g["name"])]

        # Pass 2 — only for misses
        hits2 = {}
        if need_pass2:
            hits2 = search_pass(need_pass2, 2, raw_cache)

        # Merge Google hits + run AMF on everything
        updates = []
        for g in batch:
            hit = hits1.get(g["name"]) or hits2.get(g["name"])
            if hit:
                # Google found the person — get their email via /find-email/person
                found_total += 1
                label = "P1" if hits1.get(g["name"]) else "P2"
                amf = amf_find_person_email(hit["name"], g["domain"]) if g["domain"] else {"email": None}
                email = amf.get("email") or ""
                print(f"  ✓[{label}] [{g['target']:9s}] {g['name'][:32]:32s} -> {hit['name']} ({hit['title']}) email={'✓' if email else '✗'}")
                for rn in g["row_nums"]:
                    updates += [
                        {"range": f"'{g['tab']}'!{col_letter(COL_DM_NAME)}{rn}",    "values": [[hit["name"]]]},
                        {"range": f"'{g['tab']}'!{col_letter(COL_DM_TITLE)}{rn}",   "values": [[hit["title"]]]},
                        {"range": f"'{g['tab']}'!{col_letter(COL_DM_LINKEDIN)}{rn}","values": [[hit["url"]]]},
                        {"range": f"'{g['tab']}'!{col_letter(COL_DM_EMAIL)}{rn}",   "values": [[email]]},
                    ]
            else:
                # Google missed — try AMF /decision-maker to find name + email together
                amf = amf_find_dm(g["domain"], g["name"], g["target"]) if g["domain"] else None
                if amf:
                    found_total += 1
                    email = amf.get("email") or ""
                    print(f"  ✓[AM] [{g['target']:9s}] {g['name'][:32]:32s} -> {amf['name']} ({amf['title']}) email={'✓' if email else '✗'}")
                    for rn in g["row_nums"]:
                        updates += [
                            {"range": f"'{g['tab']}'!{col_letter(COL_DM_NAME)}{rn}",    "values": [[amf["name"]]]},
                            {"range": f"'{g['tab']}'!{col_letter(COL_DM_TITLE)}{rn}",   "values": [[amf["title"]]]},
                            {"range": f"'{g['tab']}'!{col_letter(COL_DM_LINKEDIN)}{rn}","values": [[amf["linkedin"]]]},
                            {"range": f"'{g['tab']}'!{col_letter(COL_DM_EMAIL)}{rn}",   "values": [[email]]},
                        ]
                else:
                    miss_total += 1
                    reason = "no domain for AMF" if not g["domain"] else "no match (Google P1+P2+AMF)"
                    print(f"  ✗    [{g['target']:9s}] {g['name'][:34]:34s} -> {reason}")

        write_updates(service, sheet_id, updates)
        print(f"  -- batch {start // BATCH_SIZE + 1}: running {found_total} found / {miss_total} missed --")
        time.sleep(1.0)

    print(f"\n=== Done ===  Found {found_total} / {len(all_companies)}  (missing {miss_total})")


if __name__ == "__main__":
    main()
