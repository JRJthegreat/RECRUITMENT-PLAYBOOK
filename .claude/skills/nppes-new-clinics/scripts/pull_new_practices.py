"""
Phase 1 — pull newly-enumerated organization NPIs from NPPES weekly files.

Downloads every NPPES weekly incremental V2 file whose week overlaps the
lookback window (settings.window_days, default 90), streams each npidata
CSV, and upserts rows that pass ALL filters into data/nppes.db:

  Entity Type Code == 2            (organizations, drops deactivation stubs)
  not deactivated                  (deactivation date empty or reactivated)
  Provider Enumeration Date        within the window  <- the trigger
  Practice Location State          in settings.states
  primary taxonomy                 matches config/taxonomy_allowlist.json
                                   (switch=Y, fallback Code_1)

DBA names are joined from the othername_pfile inside the same zip (type 3
preferred). Already-stored NPIs are skipped (idempotent; last_update_date
refreshed). No API keys, no credits — CMS downloads only.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/pull_new_practices.py
  ... [--days 90] [--states IN,TX] [--dry_run] [--limit N]

--dry_run parses and prints stage counts but writes nothing.
--limit caps INSERTED rows (testing only — leaves the window incomplete).
"""
import argparse
import csv
import io
import os
import sys
import zipfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import (NPPES_BASE, RAW_DIR, download, ensure_dirs, get_db,
                          load_allowlist, load_denylist, load_settings,
                          log_run, match_taxonomy, normalize_address,
                          parse_nppes_date, resolve_indices, resolve_states)

NPI_COLS = [
    "NPI", "Entity Type Code",
    "Provider Organization Name (Legal Business Name)",
    "Provider First Line Business Mailing Address",
    "Provider Second Line Business Mailing Address",
    "Provider Business Mailing Address City Name",
    "Provider Business Mailing Address State Name",
    "Provider Business Mailing Address Postal Code",
    "Provider First Line Business Practice Location Address",
    "Provider Second Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "Provider Business Practice Location Address Telephone Number",
    "Provider Business Practice Location Address Fax Number",
    "Provider Enumeration Date", "Last Update Date",
    "NPI Deactivation Date", "NPI Reactivation Date",
    "Authorized Official Last Name", "Authorized Official First Name",
    "Authorized Official Title or Position",
    "Authorized Official Telephone Number",
    "Authorized Official Credential Text",
    "Is Organization Subpart", "Parent Organization LBN",
    "Parent Organization TIN", "Is Sole Proprietor", "Certification Date",
]
TAXONOMY_CODE_COLS = [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)]
TAXONOMY_SWITCH_COLS = [f"Healthcare Provider Primary Taxonomy Switch_{i}" for i in range(1, 16)]


def week_windows(window_start, today):
    """(monday, sunday) pairs for every completed week overlapping the window,
    newest first. A week's file posts the Monday AFTER the week ends."""
    monday = today - timedelta(days=today.weekday())  # this week's Monday
    windows = []
    wk_mon = monday - timedelta(days=7)               # newest completed week
    while wk_mon + timedelta(days=6) >= window_start:
        windows.append((wk_mon, wk_mon + timedelta(days=6)))
        wk_mon -= timedelta(days=7)
    return windows


def weekly_zip_name(mon, sun):
    return (f"NPPES_Data_Dissemination_{mon.strftime('%m%d%y')}_"
            f"{sun.strftime('%m%d%y')}_Weekly_V2.zip")


def load_dbas(zf):
    """othername_pfile: NPI -> DBA (type code 3 preferred, else first seen)."""
    dbas = {}
    names = [n for n in zf.namelist()
             if n.startswith("othername_pfile_") and "fileheader" not in n]
    if not names:
        return dbas
    with zf.open(names[0]) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
        header = next(reader)
        idx = resolve_indices(header, [
            "NPI", "Provider Other Organization Name",
            "Provider Other Organization Name Type Code"])
        for row in reader:
            npi = row[idx["NPI"]]
            name = row[idx["Provider Other Organization Name"]].strip()
            typ = row[idx["Provider Other Organization Name Type Code"]].strip()
            if name and (npi not in dbas or typ == "3"):
                dbas[npi] = name
    return dbas


def primary_taxonomy(row, idx):
    """Primary taxonomy convention: switch == Y wins, fallback Code_1."""
    fallback = ""
    for code_col, sw_col in zip(TAXONOMY_CODE_COLS, TAXONOMY_SWITCH_COLS):
        code = row[idx[code_col]].strip()
        if not code:
            continue
        if not fallback:
            fallback = code
        if row[idx[sw_col]].strip().upper() == "Y":
            return code
    return fallback


def process_zip(zip_path, window_start, states, allowlist, denylist, conn,
                counts, dry_run, limit, inserted_total):
    dbas = {}
    with zipfile.ZipFile(zip_path) as zf:
        dbas = load_dbas(zf)
        names = [n for n in zf.namelist()
                 if n.startswith("npidata_pfile_") and "fileheader" not in n]
        if not names:
            print(f"  WARN no npidata csv inside {os.path.basename(zip_path)}")
            return inserted_total
        with zf.open(names[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
            header = next(reader)
            idx = resolve_indices(header, NPI_COLS + TAXONOMY_CODE_COLS + TAXONOMY_SWITCH_COLS)
            g = lambda row, col: row[idx[col]].strip()

            batch = []
            pending_updates = 0
            for row in reader:
                counts["scanned"] += 1
                if g(row, "Entity Type Code") != "2":
                    continue
                counts["orgs"] += 1
                if g(row, "NPI Deactivation Date") and not g(row, "NPI Reactivation Date"):
                    continue
                enum_date = parse_nppes_date(g(row, "Provider Enumeration Date"))
                if not enum_date or enum_date < window_start:
                    continue
                counts["in_window"] += 1
                state = g(row, "Provider Business Practice Location Address State Name")
                if state not in states:
                    continue
                counts["state_pass"] += 1
                tax_code = primary_taxonomy(row, idx)
                cat_key, cat = match_taxonomy(tax_code, allowlist, denylist)
                if not cat_key:
                    continue
                counts["taxonomy_pass"] += 1

                npi = g(row, "NPI")
                existing = conn.execute(
                    "SELECT npi FROM practices WHERE npi = ?", (npi,)).fetchone()
                if existing:
                    counts["already_known"] += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE practices SET last_update_date = ? WHERE npi = ?",
                            (g(row, "Last Update Date"), npi))
                        pending_updates += 1
                        if pending_updates >= 10:   # don't hold the write lock
                            conn.commit()
                            pending_updates = 0
                    continue

                if limit and inserted_total + len(batch) >= limit:
                    continue
                addr1 = g(row, "Provider First Line Business Practice Location Address")
                addr2 = g(row, "Provider Second Line Business Practice Location Address")
                zip_code = g(row, "Provider Business Practice Location Address Postal Code")
                batch.append((
                    npi,
                    g(row, "Provider Organization Name (Legal Business Name)"),
                    dbas.get(npi, ""),
                    enum_date.isoformat(),
                    g(row, "Last Update Date"),
                    g(row, "Certification Date"),
                    addr1, addr2,
                    g(row, "Provider Business Practice Location Address City Name"),
                    state,
                    zip_code[:5],
                    g(row, "Provider Business Practice Location Address Telephone Number"),
                    g(row, "Provider Business Practice Location Address Fax Number"),
                    g(row, "Provider First Line Business Mailing Address"),
                    g(row, "Provider Second Line Business Mailing Address"),
                    g(row, "Provider Business Mailing Address City Name"),
                    g(row, "Provider Business Mailing Address State Name"),
                    g(row, "Provider Business Mailing Address Postal Code")[:5],
                    g(row, "Authorized Official First Name"),
                    g(row, "Authorized Official Last Name"),
                    g(row, "Authorized Official Title or Position"),
                    g(row, "Authorized Official Telephone Number"),
                    g(row, "Authorized Official Credential Text"),
                    tax_code, cat_key, cat["label"],
                    g(row, "Is Organization Subpart"),
                    g(row, "Parent Organization LBN"),
                    g(row, "Parent Organization TIN"),
                    g(row, "Is Sole Proprietor"),
                    normalize_address(addr1, addr2, zip_code),
                    date.today().isoformat(),
                    os.path.basename(zip_path),
                ))
                # batch-of-10: commit as we go so a crash loses at most 10 rows
                if len(batch) >= 10:
                    inserted_total = flush(conn, batch, counts, dry_run, inserted_total)
            inserted_total = flush(conn, batch, counts, dry_run, inserted_total)
            if not dry_run:
                conn.commit()
    return inserted_total


INSERT_SQL = """INSERT OR IGNORE INTO practices (
  npi, org_name, dba_name, enumeration_date, last_update_date, certification_date,
  addr1, addr2, city, state, zip, practice_phone, practice_fax,
  mailing_addr1, mailing_addr2, mailing_city, mailing_state, mailing_zip,
  ao_first, ao_last, ao_title, ao_phone, ao_credential,
  taxonomy_code, taxonomy_category, taxonomy_label,
  is_subpart, parent_lbn, parent_tin, sole_proprietor,
  addr_key, first_seen_date, source_file
) VALUES (%s)""" % ",".join(["?"] * 33)


def flush(conn, batch, counts, dry_run, inserted_total):
    if not batch:
        return inserted_total
    if not dry_run:
        conn.executemany(INSERT_SQL, batch)
        conn.commit()
    counts["inserted"] += len(batch)
    inserted_total += len(batch)
    batch.clear()
    return inserted_total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="lookback window (default settings.window_days)")
    ap.add_argument("--states", default=None,
                    help="comma-separated override, e.g. IN,TX — or ALL")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap inserted rows (testing only)")
    args = ap.parse_args()

    settings = load_settings()
    allowlist = load_allowlist()
    denylist = load_denylist()
    days = args.days or settings["window_days"]
    states = resolve_states(args.states or settings["states"])
    today = date.today()
    window_start = today - timedelta(days=days)

    label = "ALL US (50+DC)" if len(states) > 40 else str(sorted(states))
    print(f"[pull] window: {window_start} -> {today} ({days}d) | states: {label}"
          f"{' | DRY RUN' if args.dry_run else ''}")
    ensure_dirs()
    conn = get_db()

    windows = week_windows(window_start, today)
    print(f"[pull] {len(windows)} weekly files to cover the window")

    counts = {k: 0 for k in ("scanned", "orgs", "in_window", "state_pass",
                             "taxonomy_pass", "already_known", "inserted")}
    inserted_total = 0
    missing = []
    for mon, sun in windows:
        name = weekly_zip_name(mon, sun)
        dest = os.path.join(RAW_DIR, name)
        if not os.path.exists(dest):
            if not download(NPPES_BASE + name, dest):
                missing.append(name)
                continue
        else:
            print(f"  cached: {name}")
        try:
            inserted_total = process_zip(dest, window_start, states, allowlist,
                                         denylist, conn, counts, args.dry_run,
                                         args.limit, inserted_total)
        except zipfile.BadZipFile:
            os.remove(dest)
            print(f"  corrupt cached zip deleted: {name} — re-run to re-download")
            missing.append(name)

    summary = (f"scanned {counts['scanned']} rows -> {counts['orgs']} orgs -> "
               f"{counts['in_window']} newly enumerated -> {counts['state_pass']} in-state -> "
               f"{counts['taxonomy_pass']} on-taxonomy -> {counts['inserted']} inserted "
               f"({counts['already_known']} already known)")
    print(f"[pull] {summary}")
    if missing:
        print(f"[pull] WARN {len(missing)} weekly file(s) unavailable: {missing}")
        print("       (newest week may not be posted yet — files land Mondays ~07:15 UTC)")
    if not args.dry_run:
        log_run(conn, "pull_new_practices", summary)
    total = conn.execute("SELECT COUNT(*) c FROM practices").fetchone()["c"]
    unclassified = conn.execute(
        "SELECT COUNT(*) c FROM practices WHERE classification IS NULL").fetchone()["c"]
    print(f"[pull] store: {total} practices total, {unclassified} awaiting classification")
    conn.close()


if __name__ == "__main__":
    main()
