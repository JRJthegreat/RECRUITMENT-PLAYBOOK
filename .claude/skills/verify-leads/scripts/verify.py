"""
Audit and clean lead enrichment data in a Google Sheet.

Flags rows where:
  1. Website domain doesn't contain a brand word from the company name
  2. Website is on a known hosted platform (wixsite.com, applytojob.com, etc.)
  3. Email domain doesn't match website domain
  4. Email local part is generic (info, contact, sales, etc.)

Dry-run (default): prints flagged rows with reason.
--clear: clears website + email columns for flagged rows so they can be re-enriched.

Run:
  # Audit only (dry-run)
  python3 -W ignore verify.py \
    --sheet_url "URL" --tab "TAB" \
    --col_name 0 --col_website 12 --col_email 16

  # Clear bad rows
  python3 -W ignore verify.py \
    --sheet_url "URL" --tab "TAB" \
    --col_name 0 --col_website 12 --col_email 16 --clear
"""

import os
import re
import sys
import json
import time
import argparse
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

# Hosted platforms — domains where the company doesn't own the root domain
PLATFORM_DOMAINS = {
    "wixsite.com", "weebly.com", "squarespace.com", "godaddysites.com",
    "site123.me", "webflow.io", "carrd.co", "strikingly.com",
    "applytojob.com", "peopleasetalent.com", "iapplicants.com",
    "workable.com", "greenhouse.io", "lever.co",
    "placejoys.com", "pawnfinders.com",
}

GENERIC_EMAIL_LOCAL = {
    "info", "contact", "hello", "hi", "support", "admin", "office",
    "mail", "general", "inquiries", "inquiry", "help", "sales",
    "reception", "team", "enquiries", "main", "accounting", "billing",
    "hr", "careers", "jobs", "media", "press", "loans", "lending",
    "banking", "mortgage",
}

GENERIC_INDUSTRY_WORDS = {
    "electric", "electrical", "roofing", "roof", "construction", "builder",
    "trucking", "truck", "hauling", "plumbing", "hvac", "heating", "cooling",
    "painting", "landscaping", "lawn", "tree", "welding", "metal", "fabrication",
    "auto", "automotive", "car", "tire", "mechanic", "garage", "repair",
    "pawn", "jewelry", "fitness", "gym", "yoga", "medical", "health", "dental",
    "pharmacy", "clinic", "care", "law", "legal", "accounting", "consulting",
    "staffing", "cleaning", "pressure", "washing", "pest", "control",
    "security", "alarm", "coffee", "cafe", "restaurant", "pizza", "sushi",
    "bakery", "salon", "beauty", "spa", "massage", "print", "printing",
    "sign", "signs",
}

NOISE_WORDS = {
    "llc", "inc", "corp", "ltd", "co", "the", "of", "and", "a", "an",
    "for", "in", "at", "by", "on", "to", "or", "pllc", "dba",
    "group", "services", "service", "solutions", "systems", "enterprises",
    "professionals", "associates", "partners", "international",
    "national", "global", "american", "usa",
} | GENERIC_INDUSTRY_WORDS


def col_letter(idx):
    if idx < 26: return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


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


def get_sheet_id(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def extract_domain(url):
    if not url: return ""
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()


def email_local(email):
    if "@" not in email: return email.lower()
    return email.split("@")[0].lower()


def email_domain(email):
    if "@" not in email: return ""
    return email.split("@")[1].lower()


def brand_words(name):
    words = re.split(r"[\s,.\-&/()+]+", name.lower())
    return [w for w in words if len(w) >= 3 and w not in NOISE_WORDS]


def domain_matches_company(domain, name):
    bwords = brand_words(name)
    if not bwords:
        return False
    domain_clean = domain.replace("-", "").replace(".", "")
    return any(w in domain_clean for w in bwords)


def is_platform_domain(domain):
    return any(p in domain for p in PLATFORM_DOMAINS)


def audit_row(name, website, email):
    """Return list of (field, reason) tuples for problems found."""
    issues = []
    ws_domain = extract_domain(website) if website else ""

    if website and ws_domain:
        # Check platform hosting
        if is_platform_domain(ws_domain):
            issues.append(("website", f"hosted platform ({ws_domain})"))
        # Check brand word match
        elif not domain_matches_company(ws_domain, name):
            issues.append(("website", f"domain '{ws_domain}' has no brand word match"))

    if email:
        local = email_local(email)
        em_dom = email_domain(email)
        # Generic local part
        if local in GENERIC_EMAIL_LOCAL:
            issues.append(("email", f"generic address ({local}@...)"))
        # Email domain vs website domain mismatch
        elif ws_domain and em_dom and ws_domain != em_dom:
            issues.append(("email", f"domain mismatch (email={em_dom}, website={ws_domain})"))

    return issues


def main():
    parser = argparse.ArgumentParser(description="Audit and clean lead enrichment data")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_name", type=int, default=0)
    parser.add_argument("--col_website", type=int, required=True)
    parser.add_argument("--col_email", type=int, default=-1, help="Email column (-1 to skip)")
    parser.add_argument("--col_extra_clear", type=int, nargs="*", default=[],
                        help="Additional columns to clear when a row is flagged")
    parser.add_argument("--clear", action="store_true",
                        help="Actually clear flagged columns (default: dry-run audit only)")
    args = parser.parse_args()

    sheet_id = get_sheet_id(args.sheet_url)
    service = get_service()

    print(f"=== Verify Lead Data {'(DRY RUN)' if not args.clear else '(CLEARING BAD ROWS)'} ===\n", flush=True)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{args.tab}'!A:AZ"
    ).execute()
    rows = result.get("values", [])[1:]
    print(f"  {len(rows)} total rows\n", flush=True)

    flagged = []
    ok = 0
    for i, row in enumerate(rows):
        name = row[args.col_name] if len(row) > args.col_name else ""
        website = row[args.col_website] if len(row) > args.col_website else ""
        email = row[args.col_email] if args.col_email >= 0 and len(row) > args.col_email else ""

        if not name.strip():
            continue
        if not website.strip() and not email.strip():
            continue  # nothing to audit

        issues = audit_row(name.strip(), website.strip(), email.strip())
        if issues:
            flagged.append({"row": i+2, "name": name.strip(), "website": website.strip(),
                            "email": email.strip(), "issues": issues})
        else:
            ok += 1

    print(f"{'COMPANY':<40} {'WEBSITE':<35} ISSUE")
    print("-" * 110)
    for f in flagged:
        for field, reason in f["issues"]:
            val = f["website"] if field == "website" else f["email"]
            print(f"  {f['name'][:38]:<40} {val[:33]:<35} {field}: {reason}")
    print()
    print(f"OK: {ok}  |  Flagged: {len(flagged)}", flush=True)

    if not args.clear:
        print(f"\nRe-run with --clear to clear the flagged columns.", flush=True)
        return

    if not flagged:
        print("\nNothing to clear.", flush=True)
        return

    print(f"\nClearing flagged rows...", flush=True)
    clears = []
    cols_to_clear = {args.col_website}
    if args.col_email >= 0:
        cols_to_clear.add(args.col_email)
    cols_to_clear.update(args.col_extra_clear)

    for f in flagged:
        for col_idx in cols_to_clear:
            clears.append({"range": f"'{args.tab}'!{col_letter(col_idx)}{f['row']}", "values": [[""]]})

    for bs in range(0, len(clears), 100):
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": clears[bs:bs+100]}
        ).execute()
        time.sleep(0.5)

    print(f"Cleared {len(flagged)} rows ({len(clears)} cells).", flush=True)


if __name__ == "__main__":
    main()
