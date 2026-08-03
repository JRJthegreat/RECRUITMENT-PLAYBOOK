"""
Utility — re-apply the CURRENT taxonomy allowlist and address normalization
to rows already stored in data/nppes.db.

Needed after tuning config/taxonomy_allowlist.json or changing
normalize_address(): pull_new_practices.py skips already-known NPIs, so
config changes never self-correct stored rows. This script:

  1. Re-runs match_taxonomy on every row's taxonomy_code:
     - still matches       -> refresh taxonomy_category/label
     - no longer matches   -> classification = 'OFF_TAXONOMY' (kept, never
                              deleted; excluded from every export)
     - was OFF_TAXONOMY and matches again -> classification cleared so
       classify_practices.py picks it up fresh
  2. Recomputes addr_key from the stored address fields.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/resync_store.py [--dry_run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import (get_db, load_allowlist, load_denylist, log_run,
                          match_taxonomy, normalize_address)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    allowlist = load_allowlist()
    denylist = load_denylist()
    conn = get_db()
    rows = conn.execute(
        "SELECT npi, taxonomy_code, taxonomy_category, classification, "
        "addr1, addr2, zip, addr_key FROM practices").fetchall()

    retagged = offed = revived = rekeyed = 0
    pending = 0
    for p in rows:
        cat_key, cat = match_taxonomy(p["taxonomy_code"], allowlist, denylist)
        if cat_key is None:
            if p["classification"] != "OFF_TAXONOMY":
                offed += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE practices SET classification='OFF_TAXONOMY', "
                        "score=NULL WHERE npi=?", (p["npi"],))
        else:
            if p["classification"] == "OFF_TAXONOMY":
                revived += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE practices SET classification=NULL, score=NULL, "
                        "taxonomy_category=?, taxonomy_label=? WHERE npi=?",
                        (cat_key, cat["label"], p["npi"]))
            elif p["taxonomy_category"] != cat_key:
                retagged += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE practices SET taxonomy_category=?, "
                        "taxonomy_label=?, classification=NULL, score=NULL "
                        "WHERE npi=?", (cat_key, cat["label"], p["npi"]))
        new_key = normalize_address(p["addr1"], p["addr2"], p["zip"])
        if new_key != p["addr_key"]:
            rekeyed += 1
            if not args.dry_run:
                conn.execute("UPDATE practices SET addr_key=? WHERE npi=?",
                             (new_key, p["npi"]))
        pending += 1
        if pending >= 10:
            if not args.dry_run:
                conn.commit()
            pending = 0
    conn.commit()

    summary = (f"{len(rows)} rows: {offed} -> OFF_TAXONOMY, {revived} revived, "
               f"{retagged} re-categorized, {rekeyed} addr keys recomputed")
    print(f"[resync] {summary}{' (DRY RUN)' if args.dry_run else ''}")
    if not args.dry_run:
        log_run(conn, "resync_store", summary)
    conn.close()


if __name__ == "__main__":
    main()
