"""
Build THE campaign sheet: the commercial pool, fully shuffled, with facility
type stated in plain English.

Scope agreed with Jude 2026-08-04, after auditing what the earlier broad-prefix
config actually swept in:

  IN   A. Multi-staff clinics      (261Q* facility codes = a real site w/ staff)
       B. Physician/provider practices
       C. Home care + nursing agencies
       D. Facilities (SNF / assisted living / residential / hospital)
  OUT  E. Solo-clinician PLLCs      (individual taxonomy on an org NPI)
       F. Non-clinical / social services (251S community agency, doulas,
          case managers, adult day care) — this is where the horse stable came
          from, and it is why category labels are now per-CODE, never per-prefix.

Facility Type (col B) is the NUCC display name for the record's own primary
taxonomy code. No invented buckets: if the label says Physical Therapy
Clinic/Center, that is literally what the code is.

Order is RANDOM by explicit instruction — SQL RANDOM() then an in-process
shuffle, so there is no residual clustering by state, taxonomy or date. Reply
data then tells us what works instead of the ordering deciding it.

Layout keeps company/website/city/state/status at K/L/R/S/AB so the
exa-website-enrichment skill runs against it with its default flags.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/build_commercial_sheet.py [--dry_run]
"""
import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import DATA_DIR, get_db, log_run

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
TAB = "Leads"

CLUSTERS = {
    "Multi-staff clinic": [
        "261QM0801X", "261QM0850X", "261QM0855X", "261QF0400X", "261QM1300X",
        "261QP2300X", "261QD0000X", "261QP2000X", "261Q00000X", "261QC1500X",
        "261QR1300X", "261QH0700X", "261QA1903X", "261QR0200X", "261QM2500X",
        "261QH0100X", "261QU0200X", "261QE0002X", "261QX0100X"],
    "Physician practice": [
        "207Q00000X", "208D00000X", "207R00000X", "2084P0800X", "1223G0001X",
        "122300000X", "207V00000X", "207N00000X", "2080", "207RG0300X",
        "207QG0300X"],
    "Home care / nursing agency": [
        "253Z00000X", "251E00000X", "374U00000X", "3747P1801X", "376J00000X",
        "251J00000X", "251F00000X", "251G00000X"],
    "Facility (SNF/ALF/residential/hospital)": [
        "310400000X", "324500000X", "320800000X", "282N00000X", "314000000X",
        "313M00000X", "322D00000X", "315P00000X", "323P00000X", "3245S0500X",
        "311500000X"],
}

LEAD_TYPE = {"NEW_INDEPENDENT": "NEW PRACTICE",
             "NEW_LOCATION": "NEW LOCATION (existing brand)",
             "HEALTH_SYSTEM_EXPANSION": "NEW SITE (health system / group)"}

HEADERS = [
    "NPI",                 # A
    "Facility Type",       # B  real NUCC display name
    "Category",            # C  cluster
    "Taxonomy Code",       # D
    "Status",              # E  new vs expanding
    "Registered",          # F
    "Days Since",          # G
    "Owner Sites",         # H  multi-site operator signal
    "Owner Row #",         # I  1 = first row for this owner
    "Parent Org",          # J
    "Company Name",        # K  <- exa --col_company
    "Website",             # L  <- exa --col_website
    "Company Size",        # M
    "", "", "", "",        # N O P Q
    "City",                # R  <- exa --col_city
    "State",               # S  <- exa --col_state
    "DM Name",             # T
    "DM Title",            # U
    "LinkedIn URL",        # V
    "Email",               # W
    "First Name",          # X
    "Last Name",           # Y
    "Email Body",          # Z
    "Added to Instantly",  # AA
    "exa_status",          # AB <- exa --col_status
    "Filed By (NPPES)",    # AC
    "Filed By Title",      # AD
    "Practice Phone",      # AE
    "email_status",        # AF
    "Address",             # AG
    "Zip",                 # AH
    "Score",               # AI
]


def load_nucc():
    path = os.path.join(DATA_DIR, "nucc_taxonomy.csv")
    with open(path, encoding="utf-8", errors="replace") as f:
        return {r["Code"]: (r.get("Display Name") or r.get("Classification") or "")
                for r in csv.DictReader(f)}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--seed", type=int, default=None, help="reproducible shuffle")
    args = ap.parse_args()

    code_to_cluster = {}
    for cluster, codes in CLUSTERS.items():
        for c in codes:
            code_to_cluster[c] = cluster

    conn = get_db()
    like = " OR ".join(f"taxonomy_code LIKE '{c}%'" for c in CLUSTERS
                       for c in CLUSTERS[c]) if False else " OR ".join(
        f"taxonomy_code LIKE '{c}%'" for codes in CLUSTERS.values() for c in codes)
    rows = conn.execute(f"""
        SELECT * FROM practices
        WHERE classification IN ('NEW_INDEPENDENT','NEW_LOCATION','HEALTH_SYSTEM_EXPANSION')
          AND (solo_flag IS NULL OR solo_flag = 0)
          AND ({like})
          AND TRIM(COALESCE(org_name,'')) != ''
        ORDER BY RANDOM()""").fetchall()

    # Shuffle again in-process. SQL RANDOM() alone is fine, but a second pass
    # with an explicit RNG guarantees no residual ordering of any kind.
    rows = list(rows)
    random.Random(args.seed).shuffle(rows)

    nucc = load_nucc()
    today = date.today()
    seen_owner = {}
    values, tally_cluster, tally_status = [], {}, {}
    for p in rows:
        key = ((p["ao_first"] or "").strip().upper(), (p["ao_last"] or "").strip().upper())
        seen_owner[key] = seen_owner.get(key, 0) + 1
        cluster = next((code_to_cluster[c] for c in code_to_cluster
                        if p["taxonomy_code"].startswith(c)), "Other")
        ftype = nucc.get(p["taxonomy_code"], p["taxonomy_label"])
        status = LEAD_TYPE.get(p["classification"], p["classification"])
        tally_cluster[cluster] = tally_cluster.get(cluster, 0) + 1
        tally_status[status] = tally_status.get(status, 0) + 1

        row = [""] * len(HEADERS)
        row[0] = p["npi"]
        row[1] = ftype
        row[2] = cluster
        row[3] = p["taxonomy_code"]
        row[4] = status
        row[5] = p["enumeration_date"]
        row[6] = str((today - date.fromisoformat(p["enumeration_date"])).days)
        row[7] = str(p["owner_site_count"] or 1)
        row[8] = str(seen_owner[key])
        row[9] = p["parent_lbn"] or ""
        row[10] = p["dba_name"] or p["org_name"]
        row[17] = p["city"] or ""
        row[18] = p["state"] or ""
        row[28] = f"{p['ao_first']} {p['ao_last']}".strip()
        row[29] = p["ao_title"] or ""
        row[30] = p["practice_phone"] or p["ao_phone"] or ""
        row[32] = p["addr1"] or ""
        row[33] = p["zip"] or ""
        row[34] = str(p["score"] if p["score"] is not None else "")
        values.append(row)

    dupes = sum(1 for v in seen_owner.values() if v > 1)
    print(f"[sheet] {len(values)} rows | {len(seen_owner)} unique owners "
          f"({dupes} owners hold more than one row — filter col I = 1 to dedupe)")
    for k, v in sorted(tally_cluster.items(), key=lambda x: -x[1]):
        print(f"    {v:5d}  {k}")
    print("  " + " | ".join(f"{k}: {v}" for k, v in sorted(tally_status.items(), key=lambda x: -x[1])))
    if args.dry_run:
        print("[sheet] DRY RUN — nothing written")
        return

    svc = get_service()
    sid = create_sheet(svc, f"NPPES Commercial Pool — {today.isoformat()}", len(values))
    for i in range(0, len(values), 10):       # batch-of-10
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": values[i:i + 10]}).execute()
        if (i // 10) % 20 == 0:
            print(f"  appended {min(i+10, len(values))}/{len(values)}", end="\r")
        time.sleep(1.1)
    print(f"\n[sheet] https://docs.google.com/spreadsheets/d/{sid}/edit")
    log_run(conn, "build_commercial_sheet", f"{len(values)} rows -> {sid}")
    conn.close()


if __name__ == "__main__":
    main()
