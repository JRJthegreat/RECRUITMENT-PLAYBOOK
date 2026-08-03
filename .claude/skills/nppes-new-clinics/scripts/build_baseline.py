"""
Phase 1.5 — build the novelty baseline from the NPPES monthly full file.

The false-positive layer (classify_practices.py) needs to know whether a new
NPI's practice address already hosts healthcare activity, and whether a
similarly-named org already exists in the same state. That reference universe
comes from the monthly full file (~1.1 GB zip, ~9 GB CSV inside, 8M+ rows),
streamed straight out of the zip — never extracted, never loaded into memory.

Kept per target state:
  baseline_addresses  addr_key + NPI + entity type   (BOTH entity types — an
                      address hosting any active provider is not novel)
  baseline_orgs       NPI + state + normalized org name (entity type 2 only,
                      for fuzzy name matching)

Deactivated (never reactivated) records and deactivation stubs are skipped.
Rebuild is wholesale: existing baseline tables are dropped first. Rerun after
each monthly release (second Monday) or leave stale — classify warns if the
baseline is older than ~45 days.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/build_baseline.py
  ... [--states IN,TX] [--month_url URL] [--keep_zip]

The zip is cached in data/raw/ and deleted after a successful build unless
--keep_zip. No API keys, no credits.
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
                          load_settings, log_run, normalize_address,
                          normalize_org_name, resolve_indices, resolve_states,
                          set_meta, url_exists)

COLS = [
    "NPI", "Entity Type Code",
    "Provider Organization Name (Legal Business Name)",
    "Provider First Line Business Practice Location Address",
    "Provider Second Line Business Practice Location Address",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "NPI Deactivation Date", "NPI Reactivation Date",
]


def resolve_month_url():
    """Newest monthly V2 file, walking back up to 4 months."""
    d = date.today()
    for _ in range(4):
        name = f"NPPES_Data_Dissemination_{d.strftime('%B')}_{d.year}_V2.zip"
        url = NPPES_BASE + name
        if url_exists(url):
            return url, name, d.strftime("%Y-%m")
        d = (d.replace(day=1) - timedelta(days=1))
    raise SystemExit("[baseline] no monthly V2 file found in the last 4 months")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=None)
    ap.add_argument("--month_url", default=None,
                    help="override the auto-resolved monthly zip URL")
    ap.add_argument("--keep_zip", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    states = resolve_states(args.states or settings["states"])
    ensure_dirs()

    if args.month_url:
        url, name, month_tag = args.month_url, os.path.basename(args.month_url), "manual"
    else:
        url, name, month_tag = resolve_month_url()
    dest = os.path.join(RAW_DIR, name)
    label = "ALL US (50+DC)" if len(states) > 40 else str(sorted(states))
    print(f"[baseline] source: {name} | states: {label}")
    if not os.path.exists(dest):
        print("[baseline] downloading (~1.1 GB, one-time per month)...")
        if not download(url, dest):
            raise SystemExit("[baseline] download failed")
    else:
        print("[baseline] using cached zip")

    conn = get_db()
    # Wipe AND invalidate the completeness marker in one transaction — if this
    # rebuild dies midway, classify sees no baseline and fails safe (UNCERTAIN)
    # instead of silently trusting a partial table.
    conn.execute("DELETE FROM baseline_addresses")
    conn.execute("DELETE FROM baseline_orgs")
    conn.execute("DELETE FROM meta WHERE key IN "
                 "('baseline_month','baseline_built_at','baseline_states')")
    conn.commit()

    addr_batch, org_batch = [], []
    n_scanned = n_addr = n_org = 0
    with zipfile.ZipFile(dest) as zf:
        names = [n for n in zf.namelist()
                 if n.startswith("npidata_pfile_") and "fileheader" not in n]
        if not names:
            raise SystemExit("[baseline] no npidata csv inside the monthly zip")
        with zf.open(names[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"))
            header = next(reader)
            idx = resolve_indices(header, COLS)
            i_npi, i_ent = idx["NPI"], idx["Entity Type Code"]
            i_name = idx["Provider Organization Name (Legal Business Name)"]
            i_a1 = idx["Provider First Line Business Practice Location Address"]
            i_a2 = idx["Provider Second Line Business Practice Location Address"]
            i_st = idx["Provider Business Practice Location Address State Name"]
            i_zip = idx["Provider Business Practice Location Address Postal Code"]
            i_deact, i_react = idx["NPI Deactivation Date"], idx["NPI Reactivation Date"]

            for row in reader:
                n_scanned += 1
                if n_scanned % 1_000_000 == 0:
                    print(f"  scanned {n_scanned/1e6:.0f}M rows | "
                          f"kept {n_addr} addresses, {n_org} orgs", end="\r")
                ent = row[i_ent].strip()
                if not ent:                      # deactivation stub
                    continue
                if row[i_deact].strip() and not row[i_react].strip():
                    continue                     # dead NPI frees the address
                if row[i_st].strip() not in states:
                    continue
                npi = row[i_npi]
                addr_batch.append((
                    normalize_address(row[i_a1], row[i_a2], row[i_zip]), npi, ent))
                n_addr += 1
                if ent == "2":
                    org_batch.append((
                        npi, row[i_st].strip(), normalize_org_name(row[i_name])))
                    n_org += 1
                if len(addr_batch) >= 20000:
                    conn.executemany(
                        "INSERT INTO baseline_addresses VALUES (?,?,?)", addr_batch)
                    conn.executemany(
                        "INSERT INTO baseline_orgs VALUES (?,?,?)", org_batch)
                    conn.commit()
                    addr_batch, org_batch = [], []

        conn.executemany("INSERT INTO baseline_addresses VALUES (?,?,?)", addr_batch)
        conn.executemany("INSERT INTO baseline_orgs VALUES (?,?,?)", org_batch)
        conn.commit()

        # DBA/trade names -> baseline_orgs too. Franchise brands ("FastMed
        # Urgent Care") live in othername_pfile, not the legal name — without
        # this, a franchisee's new entity fuzzy-matches nothing.
        n_dba = 0
        on_names = [n for n in zf.namelist()
                    if n.startswith("othername_pfile_") and "fileheader" not in n]
        if on_names:
            conn.execute("CREATE TEMP TABLE tmp_on (npi TEXT, name_norm TEXT)")
            with zf.open(on_names[0]) as raw2:
                r2 = csv.reader(io.TextIOWrapper(raw2, encoding="utf-8", errors="replace"))
                h2 = next(r2)
                idx2 = resolve_indices(h2, ["NPI", "Provider Other Organization Name"])
                on_batch = []
                for row in r2:
                    nm = normalize_org_name(row[idx2["Provider Other Organization Name"]])
                    if nm:
                        on_batch.append((row[idx2["NPI"]], nm))
                    if len(on_batch) >= 20000:
                        conn.executemany("INSERT INTO tmp_on VALUES (?,?)", on_batch)
                        on_batch = []
                conn.executemany("INSERT INTO tmp_on VALUES (?,?)", on_batch)
            cur = conn.execute(
                """INSERT INTO baseline_orgs (npi, state, name_norm)
                   SELECT t.npi, b.state, t.name_norm FROM tmp_on t
                   JOIN (SELECT npi, state FROM baseline_orgs GROUP BY npi) b
                     ON b.npi = t.npi
                   GROUP BY t.npi, t.name_norm""")
            n_dba = cur.rowcount
            conn.execute("DROP TABLE tmp_on")
            conn.commit()
            print(f"\n  +{n_dba} DBA/trade names merged into the org-name baseline")
    set_meta(conn, "baseline_month", month_tag)
    set_meta(conn, "baseline_built_at", date.today().isoformat())
    set_meta(conn, "baseline_states", ",".join(sorted(states)))
    summary = (f"month {month_tag}: scanned {n_scanned} rows, kept "
               f"{n_addr} addresses / {n_org} orgs for {sorted(states)}")
    print(f"\n[baseline] {summary}")
    log_run(conn, "build_baseline", summary)
    conn.close()

    if not args.keep_zip:
        os.remove(dest)
        print("[baseline] zip deleted (pass --keep_zip to keep it)")


if __name__ == "__main__":
    main()
