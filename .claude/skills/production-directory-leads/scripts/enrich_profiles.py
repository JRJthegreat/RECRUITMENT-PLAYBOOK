"""
Phase 2.5 — visit profile detail pages for classification SURVIVORS only.

Fires the actor in visit-only mode (profileIds input): full untruncated
description, website (explicit link or fished from the blurb), phone,
LinkedIn and Vimeo URLs. Listing-only Phase 1 deliberately skips this —
per-profile visits are the slow part, so they're spent after Phase 2 has
decided who's ICP. Default selection: PRODUCTION_HOUSE rows not yet enriched;
--classes UNCERTAIN runs the refinement pass instead (their full description
often resolves the "X Films — weddings or commercials?" question, so
re-classify after with classify_directory.py --retry_uncertain).

Usage:
  python3 -W ignore .claude/skills/production-directory-leads/scripts/enrich_profiles.py [--limit N] \
      [--classes PRODUCTION_HOUSE,UNCERTAIN] [--metros LA,NYC] [--dry_run]
"""
import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from directory_common import get_db, load_settings, log_run, norm_domain

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

POLL_EVERY = 30
RUN_TIMEOUT = 4 * 3600


def wait_for_run(run_id):
    url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    waited = 0
    while waited < RUN_TIMEOUT:
        status = requests.get(url, params={"token": APIFY_TOKEN},
                              timeout=60).json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return status
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
    return "TIMEOUT"


def fetch_dataset(dataset_id):
    items, offset = [], 0
    while True:
        chunk = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_TOKEN, "offset": offset, "limit": 1000},
            timeout=120).json()
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap profiles visited this run")
    ap.add_argument("--classes", default="PRODUCTION_HOUSE",
                    help="comma-separated classifications to enrich")
    ap.add_argument("--metros", default=None)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    cfg = load_settings()
    conn = get_db()

    classes = [c.strip().upper() for c in args.classes.split(",")]
    where = (f"classification IN ({','.join('?' * len(classes))}) "
             "AND enriched_at IS NULL")
    params = list(classes)
    if args.metros:
        keys = [k.strip().upper() for k in args.metros.split(",")]
        where += f" AND metro IN ({','.join('?' * len(keys))})"
        params += keys

    ids = [r[0] for r in conn.execute(
        f"SELECT profile_id FROM companies WHERE {where} ORDER BY metro, name",
        params).fetchall()]
    if args.limit:
        ids = ids[:args.limit]
    print(f"{len(ids)} profiles to visit ({args.classes})"
          f" — ~{len(ids) * 5 // 60} min upper bound")
    if args.dry_run:
        print("Dry run — nothing fired.")
        return
    if not ids:
        return

    body = {
        "profileIds": ids,
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    resp = requests.post(f"https://api.apify.com/v2/acts/{cfg['actor_id']}/runs",
                         params={"token": APIFY_TOKEN}, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()["data"]
    run_id, dataset_id = data["id"], data["defaultDatasetId"]
    print(f"Actor run {run_id} started (dataset {dataset_id})")
    status = wait_for_run(run_id)
    if status != "SUCCEEDED":
        print(f"Run ended {status} — ingesting partial dataset anyway")

    items = fetch_dataset(dataset_id)
    updated = 0
    for it in items:
        pid = str(it.get("profile_id") or "")
        if not pid:
            continue
        website = it.get("website")
        conn.execute("""
            UPDATE companies SET
                description = COALESCE(?, description),
                website     = COALESCE(?, website),
                domain      = COALESCE(?, domain),
                phone       = COALESCE(?, phone),
                linkedin_url = COALESCE(?, linkedin_url),
                vimeo_url   = COALESCE(?, vimeo_url),
                emails      = CASE WHEN ? != '[]' THEN ? ELSE emails END,
                enriched_at = datetime('now')
            WHERE profile_id = ?
        """, (it.get("description"), website, norm_domain(website),
              it.get("phone"), it.get("linkedin_url"), it.get("vimeo_url"),
              json.dumps(it.get("emails") or []), json.dumps(it.get("emails") or []),
              pid))
        updated += 1
        if updated % 10 == 0:
            conn.commit()
    conn.commit()

    n_dom = conn.execute("SELECT COUNT(*) FROM companies WHERE domain IS NOT NULL "
                         "AND enriched_at IS NOT NULL").fetchone()[0]
    print(f"{updated} profiles enriched ({len(ids) - updated} missed); "
          f"{n_dom} enriched rows now have a domain")
    log_run(conn, "enrich_profiles", f"run {run_id} [{status}]: {updated}/{len(ids)} enriched")


if __name__ == "__main__":
    main()
