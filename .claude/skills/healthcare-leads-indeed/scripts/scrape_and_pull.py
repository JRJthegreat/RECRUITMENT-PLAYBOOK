"""
Phase 1: Orchestrate Apify Indeed scrapes → Google Sheet (US Healthcare).

Iterates keyword × city grid, calling the `valig/indeed-jobs-scraper` actor
once per combo. Filters at ingestion (≤ MAX_EMPLOYEES, 30+ days old, no
duplicate Job_Ids). Streams rows into the sheet in batches of 10 as combos
complete. Creates a new Google Sheet on first run if --sheet_url is omitted.

Usage:
  python3 -W ignore scrape_and_pull.py --sheet_url "URL" [options]
  python3 -W ignore scrape_and_pull.py  # creates a new sheet

  --sheet_url "URL"      append to existing sheet (skips creation)
  --limit 100            per-combo item cap (actor max is 1000)
  --cities "A,B,..."     comma-separated override (default NY + MD grid)
  --keywords "A,B,..."   comma-separated override (default clinical keywords)
  --min_days 30          only keep postings ≥ this many days old (0 = all)
  --workers 8            concurrent Apify runs
  --dry_run              print plan only, no Apify calls
  --yes                  skip confirmation prompt

Resume safety: existing Job_Ids in the sheet are loaded first; matching items
returned by the actor are skipped (no duplicate rows).
"""

import sys
import time
import argparse
import requests
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.errors import HttpError

from pull_dataset import (
    HEADERS, TAB_NAME, BATCH_SIZE, SHEET_TITLE,
    APIFY_API_TOKEN,
    get_sheet_id_from_url, get_google_service,
    map_to_row, create_sheet, setup_tab,
)

ACTOR_ID = "valig~indeed-jobs-scraper"
SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

DEFAULT_KEYWORDS = [
    "Family Medicine Physician",
    "Family Practice Physician",
    "Nurse Practitioner",
    "Physician Assistant",
]

# New York + Maryland cities — 22 cities × 4 keywords = 88 actor runs
DEFAULT_CITIES = [
    # New York
    "New York City, NY",
    "Brooklyn, NY",
    "Queens, NY",
    "Bronx, NY",
    "Staten Island, NY",
    "Long Island, NY",
    "White Plains, NY",
    "Yonkers, NY",
    "Buffalo, NY",
    "Rochester, NY",
    "Albany, NY",
    "Syracuse, NY",
    # Maryland
    "Baltimore, MD",
    "Rockville, MD",
    "Silver Spring, MD",
    "Bethesda, MD",
    "Gaithersburg, MD",
    "Columbia, MD",
    "Annapolis, MD",
    "Frederick, MD",
    "Germantown, MD",
    "Towson, MD",
]


def parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def run_actor(keyword, location, limit, timeout=180):
    """Fire one actor run (sync). Returns list of items or []. No retry."""
    try:
        resp = requests.post(
            SYNC_URL,
            params={"token": APIFY_API_TOKEN},
            json={
                "title": keyword,
                "location": location,
                "country": "us",
                "limit": limit,
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"  [!] {keyword} @ {location}: {type(e).__name__}: {e}")
        return []

    if resp.status_code not in (200, 201):
        print(f"  [!] {keyword} @ {location}: HTTP {resp.status_code} — {resp.text[:120]}")
        return []

    try:
        return resp.json() or []
    except ValueError:
        print(f"  [!] {keyword} @ {location}: invalid JSON response")
        return []


def filter_items(items, existing_job_ids, min_days_old=0):
    """Dedup filter only. Returns (rows, stats_dict). Size and date filtering is done downstream by classify_companies.py."""
    rows = []
    stats = {
        "total": len(items), "no_company": 0,
        "dupe_existing": 0, "kept": 0,
    }

    for item in items:
        job_id = item.get("key") or ""
        if job_id and job_id in existing_job_ids:
            stats["dupe_existing"] += 1
            continue

        emp = item.get("employer") or {}
        company_name = (emp.get("name") or "").strip()
        if not company_name:
            stats["no_company"] += 1
            continue

        rows.append((job_id, map_to_row(item)))
        stats["kept"] += 1

    return rows, stats


def load_existing_job_ids(service, sheet_id):
    """Read column A (Job_Id) to build a skip-set for resume safety."""
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A2:A50000"
        ).execute()
    except Exception as e:
        print(f"  [!] Could not read existing Job_Ids: {e}")
        return set()
    return {r[0] for r in resp.get("values", []) if r and r[0]}


def append_batch(service, sheet_id, batch, max_retries=6):
    """Append with exponential backoff on HTTP 429 (Sheets write-quota)."""
    delay = 4
    for attempt in range(max_retries):
        try:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"'{TAB_NAME}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": batch},
            ).execute()
            return
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 503) and attempt < max_retries - 1:
                print(f"  [!] Sheets {status} — backing off {delay}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                delay = min(delay * 2, 64)
                continue
            raise


def ensure_headers(service, sheet_id):
    """If A1 is empty, write the header row."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A1:A1"
    ).execute()
    vals = resp.get("values", [])
    if not vals or not vals[0]:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{TAB_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        print("  Wrote header row.")


def main():
    parser = argparse.ArgumentParser(description="Scrape Apify Indeed US Healthcare (keyword × city grid) → Google Sheet")
    parser.add_argument("--sheet_url", default="", help="Existing sheet URL (omit to create new)")
    parser.add_argument("--limit", type=int, default=1000, help="Per-combo actor item cap (max 1000)")
    parser.add_argument("--cities", default="", help="Comma-separated city override")
    parser.add_argument("--keywords", default="", help="Comma-separated keyword override")
    parser.add_argument("--min_days", type=int, default=30, help="Only keep postings ≥ this many days old (0 = all)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        sys.exit(1)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else DEFAULT_KEYWORDS
    cities = [c.strip() for c in args.cities.split(",") if c.strip()] if args.cities else DEFAULT_CITIES
    limit = max(1, min(args.limit, 1000))

    combos = [(k, c) for k in keywords for c in cities]

    print("=== Apify Indeed US Healthcare Scrape Orchestrator ===")
    print(f"Actor:     {ACTOR_ID}")
    print(f"Keywords:  {len(keywords)}  {keywords}")
    print(f"Cities:    {len(cities)}")
    for c in cities:
        print(f"           {c}")
    print(f"Combos:    {len(combos)}")
    print(f"Limit:     {limit} per combo  (max items = {len(combos) * limit:,})")
    print(f"Min days:  {args.min_days} days old (0 = no date filter)")
    print(f"Workers:   {args.workers}")
    print(f"Max size:  no cap (classify_companies.py handles filtering)")
    if args.sheet_url:
        print(f"Sheet:     {args.sheet_url}\n")
    else:
        print(f"Sheet:     [new sheet will be created]\n")

    if args.dry_run:
        print("[DRY RUN] No Apify calls made.")
        return

    if not args.yes:
        reply = input(f"Fire {len(combos)} actor runs? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return

    service = get_google_service()

    if args.sheet_url:
        sheet_id = get_sheet_id_from_url(args.sheet_url)
        ensure_headers(service, sheet_id)
    else:
        print("Creating new Google Sheet...")
        sheet_id = create_sheet(service, SHEET_TITLE)
        setup_tab(service, sheet_id)
        print(f"Sheet URL: https://docs.google.com/spreadsheets/d/{sheet_id}/edit\n")

    print("Loading existing Job_Ids...")
    existing_job_ids = load_existing_job_ids(service, sheet_id)
    print(f"  {len(existing_job_ids):,} already in sheet.\n")

    seen = set(existing_job_ids)
    pending_batch = []
    totals = {
        "runs_ok": 0, "runs_fail": 0, "raw": 0, "no_company": 0,
        "dupe_existing": 0, "dupe_session": 0, "written": 0,
    }
    t0 = time.time()

    def work(kw, city):
        items = run_actor(kw, city, limit)
        rows, stats = filter_items(items, existing_job_ids, args.min_days)
        return kw, city, items, rows, stats

    print(f"Launching {len(combos)} runs with {args.workers} workers...\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, kw, city): (kw, city) for kw, city in combos}
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            kw, city = futures[fut]
            try:
                kw_r, city_r, items, rows, stats = fut.result()
            except Exception as e:
                print(f"  [{done_count}/{len(combos)}] {kw} @ {city}: EXC {e}")
                totals["runs_fail"] += 1
                continue

            if not items and stats["total"] == 0:
                totals["runs_fail"] += 1
            else:
                totals["runs_ok"] += 1

            totals["raw"] += stats["total"]
            totals["no_company"] += stats["no_company"]
            totals["dupe_existing"] += stats["dupe_existing"]

            session_new = []
            for job_id, row in rows:
                if job_id and job_id in seen:
                    totals["dupe_session"] += 1
                    continue
                seen.add(job_id)
                session_new.append(row)

            pending_batch.extend(session_new)

            while len(pending_batch) >= BATCH_SIZE:
                chunk = pending_batch[:BATCH_SIZE]
                pending_batch = pending_batch[BATCH_SIZE:]
                try:
                    append_batch(service, sheet_id, chunk)
                    totals["written"] += len(chunk)
                except Exception as e:
                    print(f"  [!] Sheet write failed: {e}. Re-queueing {len(chunk)} rows.")
                    pending_batch = chunk + pending_batch
                    time.sleep(3)
                    break
                time.sleep(1.2)

            elapsed = int(time.time() - t0)
            print(f"  [{done_count}/{len(combos)}] {kw:28s} @ {city:22s}  "
                  f"raw={stats['total']:3d}  new={len(session_new):3d}  "
                  f"written={totals['written']:5d}  ({elapsed}s)")

    while pending_batch:
        chunk = pending_batch[:BATCH_SIZE]
        pending_batch = pending_batch[BATCH_SIZE:]
        append_batch(service, sheet_id, chunk)
        totals["written"] += len(chunk)
        time.sleep(1.2)

    elapsed = int(time.time() - t0)
    print("\n=== Summary ===")
    print(f"Runs ok / fail:        {totals['runs_ok']} / {totals['runs_fail']}")
    print(f"Raw items:             {totals['raw']:,}")
    print(f"  Skipped no company:  {totals['no_company']:,}")
    print(f"  Skipped dupe exist:  {totals['dupe_existing']:,}")
    print(f"  Skipped dupe sess:   {totals['dupe_session']:,}")
    print(f"Rows written:          {totals['written']:,}")
    print(f"Elapsed:               {elapsed}s")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
