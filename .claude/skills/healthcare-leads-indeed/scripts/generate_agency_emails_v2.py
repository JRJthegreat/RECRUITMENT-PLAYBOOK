"""
Generate competitor-framed email bodies for healthcare agency outreach.

Template (hardcoded ICP = small medical practices, role = clinical roles):
  [icebreaker]

  I saw that you and [competitor] are both helping small medical practices fill clinical roles.

  I'm currently connected with a few small medical practices struggling to fill clinical roles
  and already open to recruiter introductions.

  Would love to intro you instead of [competitor] if you're looking for fresh reqs.

  Worth a quick 15 min chat?

  Best,
  Jude

Competitor pool: loaded from external sheet (healthcare names prioritized), with col Q
  from the main sheet as fallback.

Reads:  col N (dm_email), col Q (clean_company), col R (icebreaker)
Writes: col W (email_body_v2)

Resume-safe: skips rows where col W already populated.
"""

import os
import re
import json
import time
import random
import argparse
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

COMPETITOR_SHEET_ID = "1KSghKyl6wmOBAr4N82SQjlCj6Hk8Ggbh1RUdUkeC0dI"

HEALTH_TERMS = {"health", "medical", "med", "care", "clinic", "nurse",
                "physician", "clinical", "staffing", "healthcare"}

COL_EMAIL      = 13  # N
COL_CLEAN_CO   = 16  # Q
COL_ICEBREAKER = 17  # R
COL_BODY_V2    = 22  # W


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


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def load_competitors(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=COMPETITOR_SHEET_ID, range="A:S"
    ).execute()
    rows = result.get("values", [])[1:]

    def normalize(name):
        # Strip leading "the " article
        n = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE).strip()
        # Title-case names that are entirely lowercase or uppercase
        if n == n.lower() or n == n.upper():
            n = n.title()
        return n

    health_names = []
    other_names = []
    for row in rows:
        company_raw = row[0].strip() if row else ""
        casual = row[18].strip() if len(row) > 18 else ""
        if not casual:
            casual = company_raw.lower()
        if not casual:
            continue
        casual = normalize(casual)
        if any(t in company_raw.lower() for t in HEALTH_TERMS):
            health_names.append(casual)
        else:
            other_names.append(casual)

    # Dedupe while preserving order
    seen = set()
    pool = []
    for name in health_names + other_names:
        if name not in seen:
            seen.add(name)
            pool.append(name)
    return pool, len(health_names)


def pick_competitor(pool, exclude_name):
    candidates = [c for c in pool if c.lower() != exclude_name.lower()]
    if not candidates:
        return random.choice(pool)
    return random.choice(candidates)


def build_email(icebreaker, competitor):
    return (
        f"{icebreaker}\n\n"
        f"I saw that you and {competitor} are both helping small medical practices fill clinical roles.\n\n"
        f"I'm currently connected with a few healthcare employers struggling to fill critical vacancies "
        f"with no internal TA team, and are actively open to working with external recruiters. "
        f"Rather than running the searches myself, I've been routing these to specialized recruiters.\n\n"
        f"Instead of {competitor}, I'd love to route these your way\n\n"
        f"Worth a quick 15 min chat?\n\n"
        f"Best,\n"
        f"Jude"
    )


def main():
    ap = argparse.ArgumentParser(description="Generate competitor-framed email bodies → col W")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Reprocess rows already filled in col W")
    args = ap.parse_args()

    print("=== Generate Agency Emails v2 (competitor frame) → col W ===\n")
    svc = get_service()

    print("Loading competitor pool...")
    pool, n_health = load_competitors(svc)
    print(f"  {n_health} healthcare-prioritized + {len(pool) - n_health} others = {len(pool)} total\n")

    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab_name, sheet_gid = resolve_tab(svc, sheet_id, args.sheet_url)
    print(f"Tab: '{tab_name}'")

    # Ensure col W exists (index 22, needs 23 columns)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_gid:
            col_count = s["properties"]["gridProperties"]["columnCount"]
            break
    if col_count < 23:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet_gid, "dimension": "COLUMNS",
                "length": 23 - col_count,
            }}]}
        ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!W1",
        valueInputOption="RAW",
        body={"values": [["email_body_v2"]]},
    ).execute()

    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:W"
    ).execute()
    data_rows = result.get("values", [])[1:]
    print(f"Total rows: {len(data_rows)}")

    print(f"Competitor pool: {len(pool)} names\n")

    pending = []
    for i, row in enumerate(data_rows):
        if args.limit and len(pending) >= args.limit:
            break
        email = cell(row, COL_EMAIL)
        if not email:
            continue
        icebreaker = cell(row, COL_ICEBREAKER)
        if not icebreaker:
            continue
        existing = cell(row, COL_BODY_V2)
        if existing and not args.overwrite:
            continue
        pending.append({
            "sheet_row": i + 2,
            "clean_company": cell(row, COL_CLEAN_CO),
            "icebreaker": icebreaker,
        })

    print(f"Rows to process: {len(pending)}\n")

    if args.dry_run:
        for p in pending[:5]:
            competitor = pick_competitor(pool, p["clean_company"])
            body = build_email(p["icebreaker"], competitor)
            print(f"  Row {p['sheet_row']}: {p['clean_company']} (competitor: {competitor})")
            print(f"  Preview: {body[:200].replace(chr(10), ' ↵ ')}\n")
        print("[DRY RUN] No writes.")
        return

    BATCH = 50
    updates = []
    total_done = 0

    for i, p in enumerate(pending):
        competitor = pick_competitor(pool, p["clean_company"])
        body = build_email(p["icebreaker"], competitor)
        updates.append({
            "range": f"'{tab_name}'!{col_letter(COL_BODY_V2)}{p['sheet_row']}",
            "values": [[body]],
        })

        if (i + 1) % BATCH == 0 or (i + 1) == len(pending):
            for attempt in range(3):
                try:
                    svc.spreadsheets().values().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={"valueInputOption": "RAW", "data": updates},
                    ).execute()
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(5)
                    else:
                        print(f"  [!] Write failed: {e}")
            total_done += len(updates)
            print(f"  Wrote {total_done}/{len(pending)}")
            updates = []
            time.sleep(0.3)

    print(f"\n=== Done — {total_done} email bodies written to col W ===")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
