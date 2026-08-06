"""
Phase 1 — scrape production companies from Google Maps into the SQLite store.

Runs Apify compass/crawler-google-places once per location query (all search
terms in one run), polls to completion, and upserts results into
data/production.db keyed by place_id — re-running never duplicates and only
fills newly discovered places.

Google Maps is the source of choice here because one pass returns company
name + WEBSITE + city, which skips the entire Exa domain-resolution cost the
other pipelines pay. ProductionHub/LBB are quality-signal second passes,
not the volume source.

Rows are stored raw: classification (Phase 2) decides what is actually a
commercial production house vs wedding videographers, photo studios,
equipment rental, and one-man bands.

Usage:
  # cost/scope preview — fires nothing
  python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py --dry_run

  # scrape two metros
  python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py --metros LA,NYC

  # everything in config
  python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py

  # fallback: ingest an existing dataset (run fired earlier / from console)
  python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py \
      --dataset_id DATASET_ID --metro_key LA
"""
import argparse
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from production_common import get_db, load_settings, log_run, norm_domain

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
APIFY_TOKEN = os.environ["APIFY_API_TOKEN"]

ACTOR = "compass~crawler-google-places"
START_URL = f"https://api.apify.com/v2/acts/{ACTOR}/runs"
POLL_EVERY = 30          # seconds
RUN_TIMEOUT = 45 * 60    # per location query


def start_run(terms, location_query, country, max_per_search):
    body = {
        "searchStringsArray": terms,
        "locationQuery": location_query,
        "maxCrawledPlacesPerSearch": max_per_search,
        "language": "en",
        "skipClosedPlaces": True,
    }
    resp = requests.post(START_URL, params={"token": APIFY_TOKEN}, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["id"], data["defaultDatasetId"]


def wait_for_run(run_id):
    url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    waited = 0
    while waited < RUN_TIMEOUT:
        status = requests.get(url, params={"token": APIFY_TOKEN}, timeout=60).json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return status
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
    return "TIMEOUT_LOCAL"


def fetch_items(dataset_id):
    items, offset = [], 0
    while True:
        resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_TOKEN, "limit": 1000, "offset": offset, "format": "json"},
            timeout=120)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page)
        if len(page) < 1000:
            return items
        offset += 1000


def upsert_items(conn, items, metro_key, term_label):
    """Insert new places; keep existing rows untouched (classification etc. survives)."""
    now = datetime.now().isoformat(timespec="seconds")
    new = dupes = skipped = 0
    for it in items:
        pid = it.get("placeId")
        title = (it.get("title") or "").strip()
        if not pid or not title or it.get("permanentlyClosed"):
            skipped += 1
            continue
        website = (it.get("website") or "").strip() or None
        row = (
            pid, title, website, norm_domain(website),
            (it.get("phone") or "").strip() or None,
            it.get("street"), it.get("city"), it.get("state"), it.get("postalCode"),
            it.get("countryCode"), metro_key,
            it.get("categoryName"), ",".join(it.get("categories") or []),
            it.get("totalScore"), it.get("reviewsCount"),
            it.get("url"), term_label, now,
        )
        cur = conn.execute("""
            INSERT OR IGNORE INTO companies
            (place_id, name, website, domain, phone, street, city, state, postal,
             country, metro, category, categories, rating, reviews, maps_url,
             search_term, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)
        if cur.rowcount:
            new += 1
        else:
            dupes += 1
    conn.commit()
    return new, dupes, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metros", default="ALL", help="comma-separated metro keys (see config), or ALL")
    ap.add_argument("--max_per_search", type=int, default=None)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--dataset_id", help="ingest an existing Apify dataset instead of scraping")
    ap.add_argument("--metro_key", help="metro key to stamp on --dataset_id ingestion")
    args = ap.parse_args()

    cfg = load_settings()
    terms = cfg["search_terms"]
    max_per = args.max_per_search or cfg["max_places_per_search"]
    conn = get_db()

    if args.dataset_id:
        if not args.metro_key:
            ap.error("--dataset_id requires --metro_key")
        items = fetch_items(args.dataset_id)
        new, dupes, skipped = upsert_items(conn, items, args.metro_key, "dataset_ingest")
        print(f"Ingested dataset {args.dataset_id}: {len(items)} items -> {new} new, {dupes} dupes, {skipped} skipped")
        log_run(conn, "scrape_maps", f"dataset {args.dataset_id}: +{new}")
        return

    wanted = None if args.metros.upper() == "ALL" else {m.strip().upper() for m in args.metros.split(",")}
    metros = [m for m in cfg["metros"] if wanted is None or m["key"] in wanted]
    if not metros:
        ap.error(f"no metros matched {args.metros}")

    n_queries = sum(len(m["queries"]) for m in metros)
    ceiling = n_queries * len(terms) * max_per
    est = ceiling * cfg["cost_per_1k_places_usd"] / 1000
    print(f"Plan: {len(metros)} metros, {n_queries} location queries x {len(terms)} terms, "
          f"max {max_per}/search")
    print(f"Upper bound: {ceiling:,} places ~ ${est:,.0f} (real cost far lower — "
          f"searches overlap heavily and thin markets return under the cap)")
    if args.dry_run:
        for m in metros:
            print(f"  {m['key']:4s} {m['label']:12s} -> {m['queries']}")
        print("\nDry run — nothing fired.")
        return

    totals = {"new": 0, "dupes": 0, "skipped": 0}
    for m in metros:
        for q in m["queries"]:
            print(f"\n[{m['key']}] {q} — starting run...")
            try:
                run_id, dataset_id = start_run(terms, q, m["country"], max_per)
            except requests.RequestException as e:
                print(f"  [!] start failed: {e}")
                continue
            status = wait_for_run(run_id)
            if status != "SUCCEEDED":
                print(f"  [!] run {run_id} ended {status} — ingest later with "
                      f"--dataset_id {dataset_id} --metro_key {m['key']}")
                continue
            items = fetch_items(dataset_id)
            new, dupes, skipped = upsert_items(conn, items, m["key"], q)
            totals["new"] += new; totals["dupes"] += dupes; totals["skipped"] += skipped
            print(f"  {len(items)} places -> {new} new, {dupes} already stored, {skipped} skipped")

    total_rows = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"\nDone. +{totals['new']} new companies (store total: {total_rows:,})")
    log_run(conn, "scrape_maps", f"metros={args.metros}: +{totals['new']} (total {total_rows})")


if __name__ == "__main__":
    main()
