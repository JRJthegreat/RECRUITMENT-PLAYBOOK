"""
Free pass: scan col B (LinkedIn description) for website URLs and fill col AA
for any row where col AA is currently blank.

No Apify/LLM calls — pure regex extraction. Run before or after enrich_agencies.py.
"""

import os
import re
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

COL_DESC    = 1   # B
COL_ID      = 5   # F
COL_LINKEDIN = 9   # J
COL_WEBSITE  = 10  # K

BATCH = 50

BLOCKED_HOSTS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "simplyhired.com", "careerbuilder.com", "monster.com", "dice.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "yelp.com", "yellowpages.com", "bbb.org", "google.com", "bing.com",
    "crunchbase.com", "zoominfo.com", "apollo.io", "rocketreach.co",
    "wikipedia.org", "trustpilot.com", "bloomberg.com",
    "bamboohr.com", "workday.com", "greenhouse.io", "adp.com", "paychex.com",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
}

SHARED_SECOND_LEVEL_TLDS = {
    "co.uk", "org.uk", "net.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "co.in", "co.za", "com.br", "co.jp",
}

URL_RE = re.compile(
    r"https?://[^\s\)\]\}\|\"\'<>]{4,}"          # full URLs with scheme
    r"|www\.[a-z0-9][a-z0-9\-]{1,60}\.[a-z]{2,}" # www.example.com
    r"|(?<![/@\w])[a-z0-9][a-z0-9\-]{2,40}"       # bare domain start
    r"\.(com|org|net|io|co|biz|info|us|health|care|med|solutions|jobs|staffing|agency|services|group|consulting)",
    re.IGNORECASE,
)


def _bare_domain(raw):
    raw = raw.strip().rstrip("/.,;:!?)")
    if "://" not in raw and not raw.startswith("www."):
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower() or urlparse("https://" + raw).netloc.lower()
        host = re.sub(r"^www\.", "", host)
        return host if "." in host else ""
    except Exception:
        return ""


def _registered_domain(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def _is_blocked(host):
    reg = _registered_domain(host)
    return (
        host in BLOCKED_HOSTS or reg in BLOCKED_HOSTS
        or host in SHARED_SECOND_LEVEL_TLDS or reg in SHARED_SECOND_LEVEL_TLDS
        or not re.match(r"^[a-z0-9][a-z0-9\-.]+\.[a-z]{2,}$", host)
    )


def extract_website(description):
    """Return best website domain found in the description text, or ''."""
    if not description:
        return ""
    matches = URL_RE.findall(description)
    for raw in matches:
        domain = _bare_domain(raw)
        if domain and not _is_blocked(domain):
            return domain
    return ""


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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def main():
    ap = argparse.ArgumentParser(description="Extract website from LinkedIn description → fill col AA")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    print("=== Extract Website from Description ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, _ = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:K"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    candidates = []
    for i, row in enumerate(data_rows):
        website = row[COL_WEBSITE].strip() if len(row) > COL_WEBSITE else ""
        if website:
            continue  # already has a website
        desc = row[COL_DESC].strip() if len(row) > COL_DESC else ""
        if not desc:
            continue
        found = extract_website(desc)
        if found:
            candidates.append({"row_num": i + 2, "website": found,
                                "company": row[0] if row else ""})

    print(f"Rows with blank website: {sum(1 for r in data_rows if (r[COL_WEBSITE].strip() if len(r) > COL_WEBSITE else '') == '')}")
    print(f"Recoverable from description: {len(candidates)}\n")

    if args.dry_run:
        for c in candidates[:20]:
            print(f"  Row {c['row_num']}: {c['company'][:45]:<45} → {c['website']}")
        return

    # Write in batches of 50
    written = 0
    for b in range(0, len(candidates), BATCH):
        chunk = candidates[b:b + BATCH]
        updates = [{
            "range": f"'{tab_name}'!{col_letter(COL_WEBSITE)}{c['row_num']}",
            "values": [[c["website"]]],
        } for c in chunk]
        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
                written += len(chunk)
                print(f"  Wrote {written}/{len(candidates)}")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Write failed: {e}")
        time.sleep(0.3)

    print(f"\n=== Done — {written} websites recovered from descriptions ===")


if __name__ == "__main__":
    main()
