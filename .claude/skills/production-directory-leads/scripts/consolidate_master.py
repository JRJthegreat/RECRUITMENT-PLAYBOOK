"""
Consolidate resolved rows (Company Website filled) across all campaign batch
sheets into one master sheet.

Jude's call (2026-08-11): after 7 batches, he wanted one list to actually
work the campaign from rather than juggling per-batch sheets. This is a
one-time-per-run consolidation, NOT a new export mechanism — the per-batch
sheets (export_batch.py) remain the unit DM-finding/email-enrichment runs
against; this just gathers their resolved rows afterward. Re-running this
script rebuilds the master sheet from scratch (clears and rewrites), so it's
safe to run again after later batches resolve more rows.

Batch sheet IDs are read from data/batch_sheets.json, a manifest written by
export_batch.py (one line per batch). If a batch predates the manifest,
add it manually — a JSON list of sheet ids in this file:
  [{"batch_id": 1, "sheet_id": "..."}, ...]

Usage:
  python3 -W ignore .claude/skills/production-directory-leads/scripts/consolidate_master.py
  python3 -W ignore .claude/skills/production-directory-leads/scripts/consolidate_master.py \
      --master_url "https://docs.google.com/spreadsheets/d/EXISTING_ID/edit"
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "data", "batch_sheets.json")
TAB = "Leads"

HEADERS = [
    "Profile_Id", "Category", "Member Since", "Views", "Metro",
    "Phone", "Profile URL", "Scraped At", "Batch", "Notes",
    "Company Name", "Company Website", "Company Size", "Revenue",
    "CEO Name", "Company Description", "Benefits",
    "City", "State",
    "DM Name", "DM Title", "LinkedIn URL", "Email",
    "First Name", "Last Name", "Email Body", "Added to Instantly",
    "status",
]
COL_WEBSITE = 11  # L, 0-indexed


def get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(token=td["token"], refresh_token=td["refresh_token"],
                        token_uri=td["token_uri"], client_id=td["client_id"],
                        client_secret=td["client_secret"],
                        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]))
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def sheet_id_from_url(url):
    import re
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else url


def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return []
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def create_master(svc, title, n_rows):
    sid = svc.spreadsheets().create(body={"properties": {"title": title}},
                                    fields="spreadsheetId").execute()["spreadsheetId"]
    gid = svc.spreadsheets().get(spreadsheetId=sid).execute()["sheets"][0]["properties"]["sheetId"]
    svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": gid, "title": TAB}, "fields": "title"}},
        {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {
            "rowCount": n_rows + 100, "columnCount": len(HEADERS), "frozenRowCount": 1}},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}},
        {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS",
                                                 "startIndex": 0, "endIndex": n_rows + 100},
                                       "properties": {"pixelSize": 18}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
    ]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW",
        body={"values": [HEADERS]}).execute()
    return sid, gid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master_url", default=None,
                    help="existing master sheet to clear and rewrite; omit to create a new one")
    ap.add_argument("--batch_urls", default=None,
                    help="comma-separated sheet URLs to pull from (overrides the manifest)")
    args = ap.parse_args()

    svc = get_service()

    if args.batch_urls:
        batch_sids = [sheet_id_from_url(u.strip()) for u in args.batch_urls.split(",")]
    else:
        manifest = load_manifest()
        if not manifest:
            sys.exit(f"No manifest at {MANIFEST_PATH} and no --batch_urls given. "
                     "Pass --batch_urls with comma-separated sheet URLs.")
        batch_sids = [b["sheet_id"] for b in manifest]

    print(f"Pulling resolved rows from {len(batch_sids)} batch sheets...")
    all_rows = []
    for sid in batch_sids:
        vals = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{TAB}!A2:AB5000").execute().get("values", [])
        resolved = [r for r in vals if len(r) > COL_WEBSITE and r[COL_WEBSITE].strip()]
        print(f"  {sid}: {len(resolved)} resolved rows")
        all_rows.extend(resolved)

    # de-dupe by domain, in case the same company was ever exported twice
    seen, deduped = set(), []
    for r in all_rows:
        dom = r[COL_WEBSITE].strip().lower()
        if dom in seen:
            continue
        seen.add(dom)
        deduped.append(r)
    if len(deduped) != len(all_rows):
        print(f"  Deduped {len(all_rows) - len(deduped)} repeat domains")

    print(f"\nTotal unique resolved companies: {len(deduped)}")

    if args.master_url:
        sid = sheet_id_from_url(args.master_url)
        gid = svc.spreadsheets().get(spreadsheetId=sid).execute()["sheets"][0]["properties"]["sheetId"]
        svc.spreadsheets().values().clear(spreadsheetId=sid, range=f"{TAB}!A1:AB5000").execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW",
            body={"values": [HEADERS]}).execute()
        print(f"Cleared and rewriting existing master: https://docs.google.com/spreadsheets/d/{sid}/edit")
    else:
        title = f"Production Houses - MASTER - {date.today().isoformat()}"
        sid, gid = create_master(svc, title, len(deduped))
        print(f"Created: https://docs.google.com/spreadsheets/d/{sid}/edit")

    for start in range(0, len(deduped), 500):
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{TAB}'!A{start + 2}", valueInputOption="RAW",
            body={"values": deduped[start:start + 500]}).execute()

    print(f"\nMaster sheet: {len(deduped)} companies written.")


if __name__ == "__main__":
    main()
