"""
Phase 1 — scrape ProductionHub directory listings into the SQLite store.

Fires the custom Apify actor jude22/productionhub-directory-scraper (built
from Saad's brief: ProductionHub is "the main directory" for the commercial
production lane) over category × metro listing pages, polls to completion,
and upserts results into data/directory.db keyed by profile_id — re-running
never duplicates and only fills newly discovered profiles.

LISTING-ONLY BY DEFAULT (visitProfiles=false): listing cards already carry
name, city, description snippet and member-since — enough for Phase 2
classification. The slow per-profile visits (full description, website,
phone, LinkedIn) are spent AFTER classification, on ICP survivors only, via
enrich_profiles.py. Same credit discipline as the rest of the repo: judge
first, spend second.

Usage:
  # cost/scope preview — fires nothing
  python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py --dry_run

  # one metro, one category (first-run verification)
  python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py \
      --metros LA --categories commercial-production-companies

  # everything in config
  python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py

  # fallback: ingest an existing dataset (run fired earlier / from console)
  python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py \
      --dataset_id DATASET_ID
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

POLL_EVERY = 30          # seconds
RUN_TIMEOUT = 4 * 3600   # listing sweeps are long: many pages × 2 challenge-able loads


def start_run(cfg, targets):
    body = {
        "targets": targets,
        "maxPagesPerList": cfg["max_pages_per_list"],
        "radius": cfg["radius"],
        "visitProfiles": False,
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    resp = requests.post(f"https://api.apify.com/v2/acts/{cfg['actor_id']}/runs",
                         params={"token": APIFY_TOKEN}, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["id"], data["defaultDatasetId"]


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
        if waited % 300 == 0:
            print(f"  … still {status} ({waited // 60} min)")
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


def upsert(conn, items):
    """Insert new profiles; on conflict merge categories and fill blanks only."""
    new = dupes = 0
    for it in items:
        pid = str(it.get("profile_id") or "")
        if not pid or not it.get("name"):
            continue
        if conn.execute("SELECT 1 FROM companies WHERE profile_id=?", (pid,)).fetchone():
            dupes += 1
        else:
            new += 1
        website = it.get("website")
        row = (
            pid, it["name"], website, norm_domain(website), it.get("phone"),
            it.get("linkedin_url"), it.get("vimeo_url"),
            json.dumps(it.get("emails") or []),
            it.get("city"), it.get("region"), it.get("country"), it.get("metro"),
            it.get("category"), json.dumps(it.get("categories") or []),
            it.get("member_since"), it.get("views"), it.get("description"),
            it.get("profile_url"), it.get("source") or "productionhub",
            it.get("date_scraped"),
        )
        conn.execute("""
            INSERT INTO companies (profile_id, name, website, domain, phone,
                linkedin_url, vimeo_url, emails, city, region, country, metro,
                category, categories, member_since, views, description,
                profile_url, source, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(profile_id) DO UPDATE SET
                categories = CASE WHEN instr(companies.categories, excluded.category) = 0
                    THEN json_insert(companies.categories, '$[#]',
                                     json_extract(excluded.categories, '$[0]'))
                    ELSE companies.categories END,
                description = COALESCE(companies.description, excluded.description),
                city = COALESCE(companies.city, excluded.city),
                region = COALESCE(companies.region, excluded.region)
        """, row)
    conn.commit()
    return new, dupes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metros", default=None, help="comma-separated metro keys (default: all)")
    ap.add_argument("--categories", default=None, help="comma-separated category slugs (default: all)")
    ap.add_argument("--dataset_id", default=None, help="ingest an existing Apify dataset instead of scraping")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    cfg = load_settings()
    conn = get_db()

    if args.dataset_id:
        items = fetch_dataset(args.dataset_id)
        before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        upsert(conn, items)
        after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        print(f"Ingested dataset {args.dataset_id}: {len(items)} items, "
              f"{after - before} new profiles ({after} total in store)")
        log_run(conn, "scrape_directory", f"dataset {args.dataset_id}: +{after - before}")
        return

    metros = cfg["metros"]
    if args.metros:
        keys = {k.strip().upper() for k in args.metros.split(",")}
        metros = [m for m in metros if m["key"] in keys]
    cats = cfg["categories"]
    if args.categories:
        want = {c.strip() for c in args.categories.split(",")}
        cats = [c for c in cats if c in want]

    targets = [{"categorySlug": c, "locationPath": m["location_path"], "metroKey": m["key"]}
               for m in metros for c in cats]
    est_pages = len(targets) * cfg["max_pages_per_list"]
    print(f"{len(targets)} targets ({len(metros)} metros × {len(cats)} categories), "
          f"≤{est_pages} listing pages (~{est_pages * 4 // 60} min upper bound)")
    if args.dry_run:
        for t in targets:
            print(f'  {t["metroKey"]:4s} {t["categorySlug"]}')
        print("Dry run — nothing fired.")
        return

    run_id, dataset_id = start_run(cfg, targets)
    print(f"Actor run {run_id} started (dataset {dataset_id})")
    status = wait_for_run(run_id)
    if status != "SUCCEEDED":
        print(f"Run ended {status} — ingesting whatever landed in the dataset anyway")

    items = fetch_dataset(dataset_id)
    before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    upsert(conn, items)
    after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"{len(items)} items -> {after - before} new profiles ({after} total in store)")
    log_run(conn, "scrape_directory",
            f"run {run_id} [{status}]: {len(items)} items, +{after - before}")


if __name__ == "__main__":
    main()
