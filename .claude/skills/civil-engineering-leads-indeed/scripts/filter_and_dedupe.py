"""
Phase 1.5: Filter unwanted titles + dedupe by company (keep highest-value role per company)

Removal patterns (case-insensitive, word-boundary on short tokens):
  Graduate, Trainee, Apprentice, Assistant, Head of, Chief, VP

Dedup — one row per normalized company. Winner picked by tier (desc), then salary_max (desc),
then date_published (desc):
  1. Associate
  2. Mid-level (plain Civil/Highways/Structural/Bridge/Drainage/Design/Project/Site Engineer)
  3. Project Manager / Contracts Manager / Sub Agent
  4. Senior
  5. Lead
  6. Principal
  7. Director
  8. Junior

Company name normalization strips: Ltd, Limited, PLC, Group, Holdings, LLP, LLC,
Inc, Corp, & Co / and Co, (UK), punctuation, whitespace.

Safety:
  - Reads all rows, decides keep/drop in-memory, then deletes bottom-up
  - --dry_run prints plan without touching the sheet
"""

import os
import re
import json
import argparse
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

TAB_NAME = "Leads"

# Column indices (0-based, matching HEADERS in pull_dataset.py)
COL_JOB_TITLE = 1      # B
COL_SALARY_MAX = 6     # G
COL_DATE_PUBLISHED = 4 # E
COL_COMPANY_NAME = 10  # K

# --- Removal patterns ---
REMOVE_PATTERNS = [
    r"\bgraduate\b",
    r"\btrainee\b",
    r"\bapprentice\b",
    r"\bassistant\b",
    r"\bhead\s+of\b",
    r"\bchief\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
]


def should_remove(title):
    t = (title or "").lower()
    return any(re.search(p, t) for p in REMOVE_PATTERNS)


# --- Tier classification (higher = better dedup winner) ---

def classify_tier(title):
    t = (title or "").lower()
    if re.search(r"\bassociate\b", t):
        return 8
    if re.search(r"\b(project\s+manager|contracts?\s+manager|sub[- ]?agent)\b", t):
        return 6
    if re.search(r"\bsenior\b", t) or re.search(r"\bsnr\b", t) or re.search(r"\bsr\.?\b", t):
        return 5
    if re.search(r"\blead\b", t):
        return 4
    if re.search(r"\bprincipal\b", t):
        return 3
    if re.search(r"\bdirector\b", t):
        return 2
    if re.search(r"\bjunior\b", t) or re.search(r"\bjnr\b", t) or re.search(r"\bjr\.?\b", t):
        return 1
    # Default: plain mid-level engineer (Civil/Highways/Structural/etc. with no seniority modifier)
    return 7


TIER_LABEL = {
    8: "Associate", 7: "Mid-level", 6: "PM/CM/SubAgent",
    5: "Senior", 4: "Lead", 3: "Principal", 2: "Director", 1: "Junior",
}


# --- Company name normalization ---

COMPANY_SUFFIX_PATTERNS = [
    r"\blimited\b", r"\bltd\.?\b", r"\bplc\b", r"\bllp\b", r"\bllc\b",
    r"\binc\.?\b", r"\bcorp\.?\b", r"\bcorporation\b",
    r"\bgroup\b", r"\bholdings?\b", r"\binternational\b",
    r"\b&\s*co\b", r"\band\s+co\b", r"\bcompany\b",
    r"\(uk\)", r"\buk\b",
]


def normalize_company(name):
    if not name:
        return ""
    s = name.lower().strip()
    for p in COMPANY_SUFFIX_PATTERNS:
        s = re.sub(p, " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- Salary parse ---

def parse_salary(s):
    if not s:
        return 0
    digits = re.sub(r"[^\d]", "", str(s))
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


# --- Date parse (lexicographic works for ISO-like strings; fallback 0) ---

def date_sort_key(s):
    return (s or "").strip()


# --- Google Sheets ---

def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"], client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    return build("sheets", "v4", credentials=creds)


def get_tab_sheet_id(service, spreadsheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Tab {tab_name!r} not found")


def main():
    parser = argparse.ArgumentParser(description="Filter + dedupe civil engineering leads sheet")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--dry_run", action="store_true", help="Show plan, don't delete")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    print("=== Filter + Dedupe Civil Engineering Leads ===")
    print(f"Sheet: {spreadsheet_id}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    # Read all data rows
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:AC10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    # Classify every row: mark for removal, or tag with (norm_company, tier, salary, date)
    plan = []  # list of dicts: {sheet_row, title, company_norm, company_raw, action: 'KEEP'|'REMOVE_FILTER'|'REMOVE_DUPE', tier, tier_label}
    for i, row in enumerate(rows):
        sheet_row = i + 2  # +2 for header row
        title = row[COL_JOB_TITLE] if len(row) > COL_JOB_TITLE else ""
        company = row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else ""
        salary = parse_salary(row[COL_SALARY_MAX] if len(row) > COL_SALARY_MAX else "")
        date = date_sort_key(row[COL_DATE_PUBLISHED] if len(row) > COL_DATE_PUBLISHED else "")

        if should_remove(title):
            plan.append({
                "sheet_row": sheet_row, "title": title, "company_raw": company,
                "action": "REMOVE_FILTER", "tier": 0, "salary": salary, "date": date,
            })
            continue

        tier = classify_tier(title)
        plan.append({
            "sheet_row": sheet_row, "title": title, "company_raw": company,
            "company_norm": normalize_company(company),
            "action": "PENDING_DEDUPE", "tier": tier, "salary": salary, "date": date,
        })

    # --- Stats: removal by filter ---
    removed_filter = [p for p in plan if p["action"] == "REMOVE_FILTER"]
    print(f"Remove by filter: {len(removed_filter)}")

    # --- Dedupe: group PENDING by company_norm, pick winner ---
    survivors = [p for p in plan if p["action"] == "PENDING_DEDUPE"]
    groups = {}
    for p in survivors:
        key = p["company_norm"] or f"__row_{p['sheet_row']}__"  # empty company treated unique
        groups.setdefault(key, []).append(p)

    dupes_removed = 0
    winners = []
    for key, items in groups.items():
        items.sort(key=lambda x: (x["tier"], x["salary"], x["date"]), reverse=True)
        winner = items[0]
        winner["action"] = "KEEP"
        winners.append(winner)
        for loser in items[1:]:
            loser["action"] = "REMOVE_DUPE"
            dupes_removed += 1

    print(f"Dedup losers:    {dupes_removed}")
    print(f"Final kept:      {len(winners)}")
    print(f"Companies:       {len(groups)}")

    # --- Tier breakdown of winners ---
    tier_counts = {}
    for w in winners:
        tier_counts[w["tier"]] = tier_counts.get(w["tier"], 0) + 1
    print("\nWinner tier breakdown:")
    for t in sorted(tier_counts.keys(), reverse=True):
        print(f"  Tier {t} ({TIER_LABEL.get(t, '?'):15s}): {tier_counts[t]}")

    # --- Sample removals ---
    print("\nSample filter removals (first 10):")
    for p in removed_filter[:10]:
        print(f"  row {p['sheet_row']}: {p['title']!r}  @ {p['company_raw']}")

    print("\nSample dupe removals (first 10):")
    dupes = [p for p in plan if p["action"] == "REMOVE_DUPE"]
    for p in dupes[:10]:
        print(f"  row {p['sheet_row']}: tier {p['tier']} {p['title']!r}  @ {p['company_raw']}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # --- Delete rows (bottom-up to preserve indices) ---
    to_delete = sorted(
        [p["sheet_row"] for p in plan if p["action"] in ("REMOVE_FILTER", "REMOVE_DUPE")],
        reverse=True,
    )
    print(f"\nDeleting {len(to_delete)} rows (bottom-up)...")

    requests = [
        {"deleteDimension": {"range": {
            "sheetId": tab_sheet_id, "dimension": "ROWS",
            "startIndex": r - 1, "endIndex": r,
        }}}
        for r in to_delete
    ]

    BATCH = 100
    for i in range(0, len(requests), BATCH):
        chunk = requests[i:i + BATCH]
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": chunk}
        ).execute()
        print(f"  Deleted chunk {i // BATCH + 1}/{(len(requests) + BATCH - 1) // BATCH}")

    # Verify
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
    ).execute()
    remaining = len(result.get("values", []))
    print(f"\nRows remaining: {remaining}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
