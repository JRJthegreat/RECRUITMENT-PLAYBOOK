"""
Phase 3 — export scored leads to CSV (and optionally a Google Sheet).

Reads classified practices from data/nppes.db, applies filters, sorts by
score descending, and writes data/output/nppes_new_practices_YYYY-MM-DD.csv
with the spec's column set. Rows already contacted are excluded by default.

Optionally mirrors the same rows to a Google Sheet (--to_sheet creates one;
--sheet_url appends to an existing one, skipping NPIs already in column A).
Sheet writes follow the repo rules: batch-of-10 appends, 18px row heights.

Taxonomy display names come from the NUCC CSV (auto-downloaded once into
data/; falls back to the allowlist label if NUCC is unreachable).

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/export_leads.py
  ... [--min_score N] [--classification NEW_INDEPENDENT,NEW_LOCATION]
  ... [--states IN,TX] [--exclude_solo] [--include_contacted] [--limit N]
  ... [--to_sheet | --sheet_url URL] [--dry_run]
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import (DATA_DIR, OUTPUT_DIR, SSL_CTX, ensure_dirs, get_db,
                          load_settings, log_run)

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
TAB_NAME = "Leads"
SHEET_TITLE = "NPPES New Practices"

CSV_COLUMNS = [
    "npi", "organization_name", "dba_name", "classification", "score",
    "enumeration_date", "days_since_enumeration",
    "taxonomy_code", "taxonomy_description", "specialty_category",
    "practice_address_1", "practice_address_2", "city", "state", "postal_code",
    "practice_phone", "practice_fax",
    "authorized_official_name", "authorized_official_title",
    "authorized_official_phone",
    "domain", "email", "email_source", "email_verified", "outreach_channel",
    "is_subpart", "parent_org", "address_is_novel", "name_similarity_score",
    "first_seen_date", "run_date",
    "solo_flag", "solo_reason", "ao_credential", "name_match_name",
]


def load_nucc():
    """NPPES taxonomy code -> NUCC display name, cached in data/."""
    ensure_dirs()
    cache = os.path.join(DATA_DIR, "nucc_taxonomy.csv")
    if not os.path.exists(cache):
        import urllib.request
        for url in load_settings()["nucc_csv_urls"]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                    data = r.read()
                if len(data) > 100_000:      # tiny file = error page
                    with open(cache, "wb") as f:
                        f.write(data)
                    print(f"  NUCC taxonomy cached from {os.path.basename(url)}")
                    break
            except Exception:
                continue
    lookup = {}
    if os.path.exists(cache):
        with open(cache, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                lookup[row["Code"]] = row.get("Display Name") or row.get("Classification", "")
    else:
        print("  WARN NUCC download failed — using allowlist labels")
    return lookup


def to_record(p, nucc, today):
    days = (today - date.fromisoformat(p["enumeration_date"])).days
    email = p["email"] or ""
    return {
        "npi": p["npi"],
        "organization_name": p["org_name"],
        "dba_name": p["dba_name"],
        "classification": p["classification"],
        "score": p["score"],
        "enumeration_date": p["enumeration_date"],
        "days_since_enumeration": days,
        "taxonomy_code": p["taxonomy_code"],
        "taxonomy_description": nucc.get(p["taxonomy_code"], p["taxonomy_label"]),
        "specialty_category": p["taxonomy_category"],
        "practice_address_1": p["addr1"],
        "practice_address_2": p["addr2"],
        "city": p["city"],
        "state": p["state"],
        "postal_code": p["zip"],
        "practice_phone": p["practice_phone"],
        "practice_fax": p["practice_fax"],
        "authorized_official_name": f"{p['ao_first']} {p['ao_last']}".strip(),
        "authorized_official_title": p["ao_title"],
        "authorized_official_phone": p["ao_phone"],
        "domain": p["domain"] or "",
        "email": email,
        "email_source": p["email_source"] or "",
        "email_verified": p["email_verified"] or "",
        "outreach_channel": "email_outreach" if email else "phone_first",
        "is_subpart": p["is_subpart"],
        "parent_org": p["parent_lbn"],
        "address_is_novel": "" if p["addr_is_novel"] is None else p["addr_is_novel"],
        "name_similarity_score": p["name_similarity"] or 0,
        "first_seen_date": p["first_seen_date"],
        "run_date": today.isoformat(),
        "solo_flag": p["solo_flag"] or 0,
        "solo_reason": p["solo_reason"] or "",
        "ao_credential": p["ao_credential"] or "",
        "name_match_name": p["name_match_name"] or "",
    }


# ---------- Google Sheets (lazy imports; only used with --to_sheet/--sheet_url) ----------

def get_google_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    with open(TOKEN_PATH) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(token_data, f)
    return build("sheets", "v4", credentials=creds)


def extract_sheet_id(url):
    if "/d/" in url:
        return url.split("/d/")[1].split("/")[0]
    return url


def create_sheet(service, title):
    resp = service.spreadsheets().create(
        body={"properties": {"title": title}}, fields="spreadsheetId").execute()
    sheet_id = resp["spreadsheetId"]
    print(f"  Created: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = meta["sheets"][0]["properties"]["sheetId"]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": gid, "title": TAB_NAME},
                "fields": "title"}},
            {"updateSheetProperties": {
                "properties": {"sheetId": gid,
                               "gridProperties": {"rowCount": 30000}},
                "fields": "gridProperties.rowCount"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 30000},
                "properties": {"pixelSize": 18},
                "fields": "pixelSize"}},
        ]}).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A1",
        valueInputOption="RAW", body={"values": [CSV_COLUMNS]}).execute()
    return sheet_id


def existing_npis(service, sheet_id):
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A2:A").execute()
    return {r[0] for r in resp.get("values", []) if r}


def append_batch(service, sheet_id, values, max_retries=6):
    """Append with 429/503 exponential backoff — repo-standard pattern."""
    from googleapiclient.errors import HttpError
    delay = 4
    for attempt in range(max_retries):
        try:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": values}).execute()
            return
        except HttpError as e:
            if e.resp.status in (429, 503) and attempt < max_retries - 1:
                print(f"\n  HTTP {e.resp.status} — retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 64)
            else:
                raise


def push_to_sheet(service, sheet_id, records):
    skip = existing_npis(service, sheet_id)
    rows = [[str(rec[c]) for c in CSV_COLUMNS]
            for rec in records if rec["npi"] not in skip]
    print(f"  sheet: {len(records) - len(rows)} already present, appending {len(rows)}")
    for i in range(0, len(rows), 10):      # batch-of-10
        append_batch(service, sheet_id, rows[i:i + 10])
        print(f"  appended {min(i + 10, len(rows))}/{len(rows)}", end="\r")
        time.sleep(1.5)
    print()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min_score", type=int, default=None)
    ap.add_argument("--classification", default=None,
                    help="comma list, e.g. NEW_INDEPENDENT,NEW_LOCATION")
    ap.add_argument("--states", default=None)
    ap.add_argument("--exclude_solo", action="store_true")
    ap.add_argument("--include_contacted", action="store_true")
    ap.add_argument("--include_exported", action="store_true",
                    help="full re-export; default emits only never-exported rows")
    ap.add_argument("--mark_contacted", default=None, metavar="FILE",
                    help="file with one NPI per line: set contacted_at and exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--to_sheet", action="store_true",
                    help="create a new Google Sheet and mirror the export")
    ap.add_argument("--sheet_url", default=None,
                    help="append to an existing sheet instead")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    conn = get_db()

    if args.mark_contacted:
        with open(args.mark_contacted) as f:
            npis = [ln.strip() for ln in f if ln.strip()]
        stamp = date.today().isoformat()
        cur = conn.executemany(
            "UPDATE practices SET contacted_at = ? WHERE npi = ? AND contacted_at IS NULL",
            [(stamp, n) for n in npis])
        conn.commit()
        print(f"[export] marked {cur.rowcount} of {len(npis)} NPIs contacted")
        conn.close()
        return

    clauses = ["classification IS NOT NULL", "classification != 'OFF_TAXONOMY'"]
    params = []
    if not args.include_contacted:
        clauses.append("contacted_at IS NULL")
    if not args.include_exported:
        clauses.append("exported_at IS NULL")
    if args.min_score is not None:
        clauses.append("score >= ?")
        params.append(args.min_score)
    if args.classification:
        cls = args.classification.split(",")
        clauses.append(f"classification IN ({','.join('?' * len(cls))})")
        params.extend(cls)
    if args.states:
        sts = args.states.split(",")
        clauses.append(f"state IN ({','.join('?' * len(sts))})")
        params.extend(sts)
    if args.exclude_solo:
        clauses.append("(solo_flag IS NULL OR solo_flag = 0)")

    rows = conn.execute(
        f"SELECT * FROM practices WHERE {' AND '.join(clauses)} "
        "ORDER BY score DESC, enumeration_date DESC", params).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    today = date.today()
    nucc = load_nucc()
    records = [to_record(p, nucc, today) for p in rows]

    by_cls, by_state = {}, {}
    for r in records:
        by_cls[r["classification"]] = by_cls.get(r["classification"], 0) + 1
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    print(f"[export] {len(records)} leads | " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_cls.items())) + " | " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())))

    if args.dry_run:
        for r in records[:20]:
            print(f"  {r['score']:+3d} {r['classification']:16s} "
                  f"{r['organization_name'][:44]:44s} {r['city']}, {r['state']} "
                  f"| {r['authorized_official_name']} ({r['authorized_official_title']})")
        print("[export] DRY RUN — nothing written")
        return

    ensure_dirs()
    out_path = os.path.join(OUTPUT_DIR, f"nppes_new_practices_{today.isoformat()}.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(records)
    print(f"[export] CSV -> {out_path}")

    if args.to_sheet or args.sheet_url:
        service = get_google_service()
        if args.sheet_url:
            sheet_id = extract_sheet_id(args.sheet_url)
        else:
            sheet_id = create_sheet(service, f"{SHEET_TITLE} — {today.isoformat()}")
        pushed = push_to_sheet(service, sheet_id, records)
        print(f"[export] sheet: {pushed} rows appended")

    conn.executemany(
        "UPDATE practices SET exported_at = ? WHERE npi = ?",
        [(today.isoformat(), r["npi"]) for r in records])
    conn.commit()
    log_run(conn, "export_leads",
            f"{len(records)} leads -> {os.path.basename(out_path)}")
    conn.close()


if __name__ == "__main__":
    main()
