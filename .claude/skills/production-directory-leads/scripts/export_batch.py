"""
Phase 3 — export a campaign batch (default 300) of PRODUCTION_HOUSE rows to
a new Google Sheet, and stamp them exported_at + batch_id in the store.

Clone of production-house-leads/export_batch.py against the directory store.
Deltas by default, nppes-style: only rows with exported_at IS NULL are
candidates, so the same company is never worked twice. One row per domain
(highest views wins — the busiest profile is the real listing); domain-less
rows are held back until enrich_profiles.py or exa-website-enrichment fills
them. ALSO deduped against the Maps store (production-house-leads
data/production.db) when present: a domain already exported from the Maps
lane is skipped here — same company, different source.

Order is RANDOM (shuffled) so reply data, not scrape order, decides what
works.

Layout is the repo's 29-col base schema: company/website/city/state at
K/L/R/S, DM block at T-W, first/last/body/pushed at X-AA, status at AB —
so apollo-dm-waterfall, exa-website-enrichment and the AMF companions run
against it with their default flags.

Usage:
  python3 -W ignore .claude/skills/production-directory-leads/scripts/export_batch.py [--size 300] \
      [--metros LA,NYC] [--dry_run]
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from directory_common import get_db, load_settings, log_run

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
MAPS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "..", "production-house-leads", "data", "production.db")
TAB = "Leads"

HEADERS = [
    "Profile_Id", "Category", "Member Since", "Views", "Metro",     # A-E
    "Phone", "Profile URL", "Scraped At", "Batch", "Notes",         # F-J
    "Company Name", "Company Website", "Company Size", "Revenue",   # K-N
    "CEO Name", "Company Description", "Benefits",                  # O-Q
    "City", "State",                                                # R-S
    "DM Name", "DM Title", "LinkedIn URL", "Email",                 # T-W
    "First Name", "Last Name", "Email Body", "Added to Instantly",  # X-AA
    "status",                                                       # AB
]


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


def create_sheet(svc, title, n_rows):
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
    print(f"  Created: https://docs.google.com/spreadsheets/d/{sid}/edit")
    return sid


def maps_exported_domains():
    """Domains already worked by the Maps lane — never double-touch a company."""
    if not os.path.exists(MAPS_DB):
        return set()
    try:
        mc = sqlite3.connect(MAPS_DB)
        return {d for (d,) in mc.execute(
            "SELECT DISTINCT domain FROM companies "
            "WHERE exported_at IS NOT NULL AND domain IS NOT NULL")}
    except sqlite3.Error:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--metros", default=None, help="restrict batch to comma-separated metro keys")
    ap.add_argument("--seed", type=int, default=None, help="reproducible shuffle")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    cfg = load_settings()
    size = args.size or cfg["export_batch_size"]
    conn = get_db()

    where = "classification='PRODUCTION_HOUSE' AND exported_at IS NULL AND domain IS NOT NULL"
    params = []
    if args.metros:
        keys = [k.strip().upper() for k in args.metros.split(",")]
        where += f" AND metro IN ({','.join('?' * len(keys))})"
        params = keys

    cols = ("profile_id", "name", "website", "domain", "phone", "city", "region",
            "metro", "category", "member_since", "views", "profile_url",
            "scraped_at", "description")
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {','.join(cols)} FROM companies WHERE {where}", params).fetchall()]

    # never re-touch a domain that already went out — from either lane
    exported = {d for (d,) in conn.execute(
        "SELECT DISTINCT domain FROM companies WHERE exported_at IS NOT NULL AND domain IS NOT NULL")}
    exported |= maps_exported_domains()

    # one row per domain — highest views wins (busiest profile = real listing)
    by_domain = {}
    for r in rows:
        if r["domain"] in exported:
            continue
        best = by_domain.get(r["domain"])
        if best is None or (r["views"] or 0) > (best["views"] or 0):
            by_domain[r["domain"]] = r
    pool = list(by_domain.values())

    rng = random.Random(args.seed)
    rng.shuffle(pool)
    batch = pool[:size]

    next_id = (conn.execute("SELECT MAX(batch_id) FROM companies").fetchone()[0] or 0) + 1
    print(f"Pool: {len(pool)} unexported production houses "
          f"({len(rows)} rows before domain-dedupe) -> batch {next_id}: {len(batch)} rows")
    if args.dry_run:
        for r in batch[:15]:
            print(f'  {r["name"]} | {r["domain"]} | {r["city"]}, {r["region"]} [{r["metro"]}]')
        print("Dry run — no sheet created, nothing stamped.")
        return
    if not batch:
        print("Nothing to export.")
        return

    svc = get_service()
    title = f"Production Houses (Directory) - Batch {next_id} - {date.today().isoformat()}"
    sid = create_sheet(svc, title, len(batch))

    values = [[
        r["profile_id"], r["category"] or "", r["member_since"] or "", r["views"] or "",
        r["metro"], r["phone"] or "", r["profile_url"] or "", r["scraped_at"] or "",
        str(next_id), "",
        r["name"], r["domain"], "", "", "",
        (r["description"] or "")[:900], "",
        r["city"] or "", r["region"] or "",
        "", "", "", "", "", "", "", "", "",
    ] for r in batch]
    for start in range(0, len(values), 100):
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{TAB}'!A{start + 2}", valueInputOption="RAW",
            body={"values": values[start:start + 100]}).execute()

    conn.executemany(
        "UPDATE companies SET exported_at=datetime('now'), batch_id=? WHERE profile_id=?",
        [(next_id, r["profile_id"]) for r in batch])
    conn.commit()
    log_run(conn, "export_batch", f"batch {next_id}: {len(batch)} rows -> {sid}")
    print(f"Batch {next_id}: {len(batch)} rows exported and stamped.")


if __name__ == "__main__":
    main()
