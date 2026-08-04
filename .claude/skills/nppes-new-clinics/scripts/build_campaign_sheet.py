"""
Build a campaign sheet from the store, laid out so the exa-website-enrichment
skill runs against it UNMODIFIED (company K, website L, city R, state S,
status AB — that skill's defaults).

Clinic type is deliberately front-loaded in columns B/C/D: the whole point of
this sheet is that a human can see at a glance what kind of practice each row
is before a single email goes out.

Selection: RANDOM within the chosen categories (Jude's call — no score bias, so
replies tell us what actually works). Solo-PLLC shells and LIKELY_ADMIN reorgs
are excluded. Rows are one-per-OWNER (a group filing 5 sites is one person to
email, not five).

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/build_campaign_sheet.py \
      --count 1200 [--categories a,b,c] [--dry_run] [--sheet_url URL]
"""
import argparse
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import get_db, log_run

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
TAB = "Leads"
# Priority order — highest first. Selection fills from the top and only reaches
# a lower tier when the ones above are exhausted.
# mental_health_counseling is EXCLUDED (Jude, 2026-07-23): biggest bucket by raw
# volume but the weakest payer — plain counseling practices commonly staff with
# 1099 contract therapists and do not pay placement fees. Psychiatry/behavioral
# CLINICS stay in: those hire psych NPs, which is the bench we are selling.
DEFAULT_CATS = ["psychiatry_behavioral", "family_medicine", "clinic_center",
                "internal_medicine", "behavioral_facilities"]

# A..AH — K/L/R/S/AB positions are fixed by the exa skill's defaults
HEADERS = [
    "NPI",                    # A
    "Clinic Type",            # B  <- human-readable, the important one
    "Specialty Detail",       # C
    "Taxonomy Code",          # D
    "Lead Type",              # E  NEW_INDEPENDENT / NEW_LOCATION / EXPANSION
    "Registered",             # F
    "Days Since",             # G
    "Score",                  # H
    "Owner Sites",            # I
    "Parent Org",             # J
    "Company Name",           # K  <- exa --col_company
    "Website",                # L  <- exa --col_website (output)
    "Company Size",           # M
    "",                       # N
    "",                       # O
    "",                       # P
    "",                       # Q
    "City",                   # R  <- exa --col_city
    "State",                  # S  <- exa --col_state
    "DM Name",                # T
    "DM Title",               # U
    "LinkedIn URL",           # V
    "Email",                  # W
    "First Name",             # X
    "Last Name",              # Y
    "Email Body",             # Z
    "Added to Instantly",     # AA
    "exa_status",             # AB <- exa --col_status
    "Filed By (NPPES)",       # AC
    "Filed By Title",         # AD
    "Practice Phone",         # AE
    "email_status",           # AF
    "Address",                # AG
    "Zip",                    # AH
]

LEAD_TYPE = {"NEW_INDEPENDENT": "NEW PRACTICE",
             "NEW_LOCATION": "NEW LOCATION (existing brand)",
             "HEALTH_SYSTEM_EXPANSION": "NEW SITE (health system/group)"}


def rank_of(cat, cats):
    return cats.index(cat) if cat in cats else 99


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


def create_sheet(svc, title):
    sid = svc.spreadsheets().create(body={"properties": {"title": title}},
                                    fields="spreadsheetId").execute()["spreadsheetId"]
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    gid = meta["sheets"][0]["properties"]["sheetId"]
    svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": gid, "title": TAB}, "fields": "title"}},
        {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"rowCount": 5000, "columnCount": len(HEADERS)}},
                                   "fields": "gridProperties(rowCount,columnCount)"}},
        {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": 5000},
                                       "properties": {"pixelSize": 18}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW",
        body={"values": [HEADERS]}).execute()
    print(f"  Created: https://docs.google.com/spreadsheets/d/{sid}/edit")
    return sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1200,
                    help="candidates to export (need ~3x your email target)")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATS))
    ap.add_argument("--sheet_url", default=None, help="append to an existing sheet")
    ap.add_argument("--replace", action="store_true",
                    help="with --sheet_url: wipe data rows first, keep headers")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    cats = args.categories.split(",")
    conn = get_db()
    rows = conn.execute(f"""
        SELECT * FROM practices
        WHERE classification IN ('NEW_INDEPENDENT','NEW_LOCATION','HEALTH_SYSTEM_EXPANSION')
          AND (solo_flag IS NULL OR solo_flag = 0)
          AND taxonomy_category IN ({','.join('?' * len(cats))})
          AND TRIM(COALESCE(org_name,'')) != ''
          AND exported_at IS NULL          -- never re-pick a row already on a sheet
        ORDER BY RANDOM()""", cats).fetchall()

    # ROUND-ROBIN across categories, random within each. Neither strict
    # priority nor scarcest-first works here: priority hands the whole quota to
    # the biggest bucket (psychiatry_behavioral alone can fill 1,200 and family
    # medicine never appears), scarcest-first does the reverse. Round-robin
    # gives every vertical a comparable cell size, which is the only way reply
    # rate by clinic type means anything, and small categories are simply
    # exhausted early while the abundant ones keep filling.
    buckets = {}
    for r in rows:                       # rows are already in random order
        buckets.setdefault(r["taxonomy_category"], []).append(r)
    order = sorted(buckets, key=lambda c: rank_of(c, cats))

    seen, picked = set(), []
    while len(picked) < args.count and any(buckets.values()):
        for cat in order:
            if not buckets[cat] or len(picked) >= args.count:
                continue
            r = buckets[cat].pop()
            # one row per OWNER — a group filing several sites is one person
            key = ((r["ao_first"] or "").strip().upper(), (r["ao_last"] or "").strip().upper())
            if key != ("", "") and key in seen:
                continue
            seen.add(key)
            picked.append(r)

    by_cat, by_type = {}, {}
    for p in picked:
        by_cat[p["taxonomy_label"]] = by_cat.get(p["taxonomy_label"], 0) + 1
        by_type[p["classification"]] = by_type.get(p["classification"], 0) + 1
    print(f"[sheet] {len(picked)} candidates selected at random from {len(rows)} eligible")
    for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"    {v:5d}  {k}")
    print("  lead types: " + ", ".join(f"{LEAD_TYPE.get(k,k)} {v}" for k, v in by_type.items()))
    if args.dry_run:
        print("[sheet] DRY RUN — nothing written")
        return

    today = date.today()
    values = []
    for p in picked:
        days = (today - date.fromisoformat(p["enumeration_date"])).days
        row = [""] * len(HEADERS)
        row[0] = p["npi"]
        row[1] = p["taxonomy_label"]
        row[2] = p["taxonomy_category"].replace("_", " ").title()
        row[3] = p["taxonomy_code"]
        row[4] = LEAD_TYPE.get(p["classification"], p["classification"])
        row[5] = p["enumeration_date"]
        row[6] = str(days)
        row[7] = str(p["score"] if p["score"] is not None else "")
        row[8] = str(p["owner_site_count"] or 1)
        row[9] = p["parent_lbn"] or ""
        row[10] = p["dba_name"] or p["org_name"]          # K company
        row[17] = p["city"]                               # R city
        row[18] = p["state"]                              # S state
        row[28] = f"{p['ao_first']} {p['ao_last']}".strip()
        row[29] = p["ao_title"] or ""
        row[30] = p["practice_phone"] or p["ao_phone"] or ""
        row[32] = p["addr1"] or ""
        row[33] = p["zip"] or ""
        values.append(row)

    svc = get_service()
    if args.sheet_url:
        sid = args.sheet_url.split("/d/")[1].split("/")[0]
        if args.replace:
            svc.spreadsheets().values().clear(
                spreadsheetId=sid, range=f"'{TAB}'!A2:AZ").execute()
            print("  cleared existing data rows")
    else:
        sid = create_sheet(svc, f"NPPES Campaign — psych/BH + primary care — {today.isoformat()}")

    for i in range(0, len(values), 10):        # batch-of-10
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": values[i:i + 10]}).execute()
        print(f"  appended {min(i+10, len(values))}/{len(values)}", end="\r")
        time.sleep(1.2)
    print()
    conn.executemany("UPDATE practices SET exported_at=? WHERE npi=?",
                     [(today.isoformat(), p["npi"]) for p in picked])
    conn.commit()
    log_run(conn, "build_campaign_sheet", f"{len(picked)} candidates -> sheet {sid}")
    print(f"[sheet] https://docs.google.com/spreadsheets/d/{sid}/edit")
    conn.close()


if __name__ == "__main__":
    main()
