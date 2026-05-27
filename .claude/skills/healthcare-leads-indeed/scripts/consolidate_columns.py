"""
One-time fix: merge duplicate linkedin_url / website columns.

The original Sales Navigator export already had:
  col J (index 9)  — linkedin_url  (1,650 rows)
  col K (index 10) — website       (1,629 rows)

enrich_agencies.py added duplicates at:
  col Z (index 25) — linkedin_url  (2,715 rows — more complete)
  col AA (index 26) — website      (2,439 rows — more complete)

This script:
  1. For each row: writes the better value into J / K
       linkedin: Z if populated, else J
       website:  AA if valid, else K if valid, else ""
  2. Deletes cols Z and AA (shift left)

Run once, then enrich_agencies.py targets J+K going forward.
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

COL_J = 9    # linkedin_url (original)
COL_K = 10   # website (original)
COL_Z = 25   # linkedin_url (added by enrich_agencies.py)
COL_AA = 26  # website (added by enrich_agencies.py)

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
    "calendly.com", "cal.com", "zoom.us", "teams.microsoft.com",
    "hubspot.com", "typeform.com", "mailchimp.com", "constantcontact.com",
    "squarespace.com", "wix.com", "weebly.com", "wordpress.com",
}

SHARED_SECOND_LEVEL_TLDS = {
    "co.uk", "org.uk", "net.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "co.in", "co.za", "com.br", "co.jp",
}


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


def _registered_domain(host):
    if not host:
        return ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def _is_valid_website(value):
    if not value or not value.strip():
        return False
    host = _bare_domain(value) or value.strip().lower().lstrip("www.")
    reg = _registered_domain(host)
    if host in BLOCKED_HOSTS or reg in BLOCKED_HOSTS:
        return False
    if host in SHARED_SECOND_LEVEL_TLDS or reg in SHARED_SECOND_LEVEL_TLDS:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9\-.]+\.[a-z]{2,}$", host))


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
    ap = argparse.ArgumentParser(description="Consolidate duplicate linkedin_url/website columns")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--dry_run", action="store_true", help="Preview first 20 rows, no writes")
    ap.add_argument("--no_delete", action="store_true", help="Merge into J+K but don't delete Z+AA")
    args = ap.parse_args()

    print("=== Consolidate linkedin_url + website columns ===\n")
    svc = get_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:AA"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}\n")

    j_count = sum(1 for r in data_rows if len(r) > COL_J and r[COL_J].strip())
    k_count = sum(1 for r in data_rows if len(r) > COL_K and r[COL_K].strip())
    z_count = sum(1 for r in data_rows if len(r) > COL_Z and r[COL_Z].strip())
    aa_count = sum(1 for r in data_rows if len(r) > COL_AA and r[COL_AA].strip())
    print(f"  Col J (linkedin_url original): {j_count} rows")
    print(f"  Col K (website original):      {k_count} rows")
    print(f"  Col Z (linkedin_url new):      {z_count} rows")
    print(f"  Col AA (website new):          {aa_count} rows\n")

    updates = []
    stats = {"linkedin_from_z": 0, "linkedin_from_j": 0, "linkedin_blank": 0,
             "website_from_aa": 0, "website_from_k": 0, "website_blank": 0,
             "website_blocked": 0}

    for i, row in enumerate(data_rows):
        row_num = i + 2

        j_val = row[COL_J].strip() if len(row) > COL_J else ""
        k_val = row[COL_K].strip() if len(row) > COL_K else ""
        z_val = row[COL_Z].strip() if len(row) > COL_Z else ""
        aa_val = row[COL_AA].strip() if len(row) > COL_AA else ""

        merged_li = z_val or j_val
        if z_val:
            stats["linkedin_from_z"] += 1
        elif j_val:
            stats["linkedin_from_j"] += 1
        else:
            stats["linkedin_blank"] += 1

        aa_valid = _is_valid_website(aa_val)
        k_valid = _is_valid_website(k_val)

        if aa_valid:
            merged_website = _bare_domain(aa_val) or aa_val
            stats["website_from_aa"] += 1
        elif k_valid:
            merged_website = _bare_domain(k_val) or k_val
            stats["website_from_k"] += 1
        else:
            merged_website = ""
            if aa_val or k_val:
                stats["website_blocked"] += 1
            else:
                stats["website_blank"] += 1

        if merged_li != j_val or merged_website != k_val:
            updates.append({
                "row_num": row_num,
                "li": merged_li,
                "website": merged_website,
                "old_j": j_val, "old_k": k_val,
                "old_z": z_val, "old_aa": aa_val,
            })

    print(f"Merge plan:")
    print(f"  linkedin_url: {stats['linkedin_from_z']} from Z, {stats['linkedin_from_j']} from J, {stats['linkedin_blank']} blank")
    print(f"  website:      {stats['website_from_aa']} from AA, {stats['website_from_k']} from K, "
          f"{stats['website_blocked']} blocked/invalid cleared, {stats['website_blank']} blank")
    print(f"  Rows that need a write: {len(updates)}\n")

    if args.dry_run:
        print("Sample rows that will change (first 20):")
        for u in updates[:20]:
            print(f"  Row {u['row_num']:4d}:")
            if u['li'] != u['old_j']:
                old_li = u['old_j'] or "(blank)"
                new_li = u['li'] or "(blank)"
                print(f"    J: {old_li[:60]!r} → {new_li[:60]!r}")
            if u['website'] != u['old_k']:
                old_w = u['old_k'] or u['old_aa'] or "(blank)"
                new_w = u['website'] or "(blank)"
                print(f"    K: {old_w[:60]!r} → {new_w[:60]!r}")
        print("\n[DRY RUN] No writes.")
        return

    written = 0
    for b in range(0, len(updates), BATCH):
        chunk = updates[b:b + BATCH]
        data = []
        for u in chunk:
            data.append({
                "range": f"'{tab_name}'!{col_letter(COL_J)}{u['row_num']}",
                "values": [[u["li"]]],
            })
            data.append({
                "range": f"'{tab_name}'!{col_letter(COL_K)}{u['row_num']}",
                "values": [[u["website"]]],
            })
        for attempt in range(3):
            try:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
                written += len(chunk)
                print(f"  Wrote {written}/{len(updates)} rows")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f"  [!] Write failed: {e}")
        time.sleep(0.3)

    print(f"\n  Merged {written} rows into J+K.")

    if args.no_delete:
        print("  --no_delete set: cols Z+AA retained.")
        print(f"\n=== Done (no column deletion) ===")
        return

    # Delete AA (index 26) first, then Z (index 25) — right to left avoids index shift
    print("\n  Deleting cols AA and Z...")
    for start_idx in [26, 25]:
        for attempt in range(3):
            try:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"deleteDimension": {
                        "range": {
                            "sheetId": sheet_gid,
                            "dimension": "COLUMNS",
                            "startIndex": start_idx,
                            "endIndex": start_idx + 1,
                        }
                    }}]},
                ).execute()
                print(f"  Deleted col {col_letter(start_idx)} (index {start_idx})")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    print(f"  [!] Delete col {start_idx} failed: {e}")
        time.sleep(0.5)

    print(f"\n=== Done — J+K consolidated, Z+AA deleted ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
