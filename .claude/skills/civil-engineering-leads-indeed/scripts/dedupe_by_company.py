"""
Phase 1.8: Dedupe by company — keep one row per company.

Rules:
  1. Group rows by normalized company name.
  2. Winner = highest seniority tier (Director > Associate > Principal > Lead >
     Senior > PM/CM/SubAgent > Mid > Junior).
  3. Tie-break: oldest Date Published (longest-open role = biggest pain signal).

Dry-run by default — prints plan. Re-run with --apply to delete losers.

Safety: reads all rows, decides in-memory, deletes bottom-up so indices stay stable.
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

# Column indices (0-based; see pull_dataset.py HEADERS)
COL_JOB_TITLE = 1       # B
COL_DATE_PUBLISHED = 4  # E
COL_COMPANY_NAME = 10   # K


# --- Seniority tier (higher = more senior = wins dedup) ---

def classify_tier(title):
    t = (title or "").lower()
    if re.search(r"\bdirector\b", t):
        return 8
    if re.search(r"\bassociate\b", t):
        return 7
    if re.search(r"\bprincipal\b", t):
        return 6
    if re.search(r"\blead\b", t):
        return 5
    if re.search(r"\b(senior|snr|sr\.?)\b", t):
        return 4
    if re.search(r"\b(project\s+manager|contracts?\s+manager|sub[- ]?agent)\b", t):
        return 3
    if re.search(r"\b(junior|jnr|jr\.?|graduate|trainee|apprentice)\b", t):
        return 1
    # Default: plain mid-level engineer (Civil/Highways/Structural/etc., no modifier)
    return 2


TIER_LABEL = {
    8: "Director", 7: "Associate", 6: "Principal", 5: "Lead",
    4: "Senior", 3: "PM/CM/SubAgent", 2: "Mid-level", 1: "Junior",
}


# --- Company name normalization (matches classify_companies.py behavior) ---

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


# --- Date sort (ISO-ish strings sort lexicographically; older = smaller) ---

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
    parser = argparse.ArgumentParser(description="Dedup civil engineering leads by company")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually delete losers. Default: dry run.")
    args = parser.parse_args()

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()
    tab_sheet_id = get_tab_sheet_id(service, spreadsheet_id, TAB_NAME)

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"=== Dedup by Company ({mode}) ===")
    print(f"Sheet: {spreadsheet_id}\n")

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:AC10000"
    ).execute()
    rows = result.get("values", [])
    print(f"Total rows: {len(rows)}")

    plan = []
    for i, row in enumerate(rows):
        sheet_row = i + 2  # +2 for header
        title = row[COL_JOB_TITLE] if len(row) > COL_JOB_TITLE else ""
        company = row[COL_COMPANY_NAME] if len(row) > COL_COMPANY_NAME else ""
        date = date_sort_key(row[COL_DATE_PUBLISHED] if len(row) > COL_DATE_PUBLISHED else "")
        plan.append({
            "sheet_row": sheet_row,
            "title": title,
            "company_raw": company,
            "company_norm": normalize_company(company),
            "tier": classify_tier(title),
            "date": date,
        })

    # Group by normalized company; empty company treated as unique
    groups = {}
    for p in plan:
        key = p["company_norm"] or f"__row_{p['sheet_row']}__"
        groups.setdefault(key, []).append(p)

    losers = []
    winners = []
    for key, items in groups.items():
        # Sort: tier DESC (higher wins), then date ASC (older wins).
        # Python sort is stable; sort by date asc first, then tier desc.
        items.sort(key=lambda x: x["date"])           # oldest first
        items.sort(key=lambda x: x["tier"], reverse=True)  # highest tier first
        winner = items[0]
        winner["action"] = "KEEP"
        winners.append(winner)
        for loser in items[1:]:
            loser["action"] = "REMOVE_DUPE"
            losers.append(loser)

    print(f"Unique companies: {len(groups)}")
    print(f"Winners (keep):   {len(winners)}")
    print(f"Losers (remove):  {len(losers)}\n")

    # Winner tier breakdown
    tier_counts = {}
    for w in winners:
        tier_counts[w["tier"]] = tier_counts.get(w["tier"], 0) + 1
    print("Winner tier breakdown:")
    for t in sorted(tier_counts.keys(), reverse=True):
        print(f"  Tier {t} ({TIER_LABEL.get(t, '?'):15s}): {tier_counts[t]}")

    # Sample multi-job companies (to sanity check)
    multi = sorted(
        [(k, v) for k, v in groups.items() if len(v) > 1],
        key=lambda kv: -len(kv[1]),
    )
    print(f"\nCompanies with >1 job: {len(multi)}")
    print("Sample (top 10 by # of jobs):")
    for key, items in multi[:10]:
        win = items[0]
        print(f"\n  {win['company_raw']!r}  ({len(items)} jobs)")
        print(f"    WINNER   tier {win['tier']:2d} ({TIER_LABEL[win['tier']]}) "
              f"{win['date']}  {win['title']!r}")
        for loser in items[1:]:
            print(f"    drop     tier {loser['tier']:2d} ({TIER_LABEL[loser['tier']]}) "
                  f"{loser['date']}  {loser['title']!r}")

    if not args.apply:
        print("\n[DRY RUN] No changes made. Re-run with --apply to delete losers.")
        return

    to_delete = sorted([l["sheet_row"] for l in losers], reverse=True)
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

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:A10000"
    ).execute()
    remaining = len(result.get("values", []))
    print(f"\nRows remaining: {remaining}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
