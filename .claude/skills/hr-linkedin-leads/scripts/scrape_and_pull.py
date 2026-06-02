"""
Phase 1: Orchestrate LinkedIn scrapes → Google Sheet (US HR specialist roles).

Iterates keyword × location grid using insight_api_labs~linkedin-jobs-scraper.
Filters at ingestion: datePublished within [min_days, max_days] window, no
duplicate Job_Ids, missing company name. Company size is NOT filtered here
(not available from LinkedIn actor) — run find_company_sizes.py after ingest.

Usage:
  python3 -W ignore scrape_and_pull.py --sheet_url "URL" [options]

  --limit 50             per-combo item cap (default 50)
  --min_days 30          minimum posting age in days (default 30)
  --max_days 45          maximum posting age in days (default 45)
  --locations "A,B,..."  comma-separated override
  --keywords "A,B,..."   comma-separated override
  --workers 5            concurrent Apify runs (default 5 — actor is slow)
  --dry_run              print plan only, no Apify calls
  --yes                  skip confirmation prompt

Resume safety: existing Job_Ids in the sheet are loaded first; matching items
are skipped (no duplicate rows).
"""

import sys
import time
import argparse
import requests
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.errors import HttpError

from pull_dataset import (
    HEADERS, TAB_NAME, BATCH_SIZE,
    APIFY_API_TOKEN,
    get_sheet_id_from_url, get_google_service,
    map_to_row,
)

ACTOR_ID = "insight_api_labs~linkedin-jobs-scraper"
RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"
DATASET_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"

DEFAULT_KEYWORDS = []   # Always specify via --keywords
DEFAULT_LOCATIONS = []  # Always specify via --locations


def parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def run_actor(keyword, location, limit, min_days, max_days, poll_interval=15, run_timeout=3600):
    """Fire one actor run (async). Returns list of items or []. No retry."""
    posted_after = (date.today() - timedelta(days=max_days)).isoformat()

    # Start the run
    try:
        resp = requests.post(
            RUN_URL,
            params={"token": APIFY_API_TOKEN},
            json={
                "jobTitle": keyword,
                "location": location,
                "postedAfter": posted_after,
                "numJobs": limit,
                "fewApplicants": True,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"  [!] {keyword} @ {location}: {type(e).__name__}: {e}")
        return []

    if resp.status_code not in (200, 201):
        print(f"  [!] {keyword} @ {location}: start HTTP {resp.status_code} — {resp.text[:120]}")
        return []

    run_data = resp.json().get("data", {})
    run_id = run_data.get("id")
    dataset_id = run_data.get("defaultDatasetId")
    if not run_id:
        print(f"  [!] {keyword} @ {location}: no run id in response")
        return []

    # Poll until finished
    status_url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs/{run_id}"
    deadline = time.time() + run_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            s = requests.get(status_url, params={"token": APIFY_API_TOKEN}, timeout=30)
            status = s.json().get("data", {}).get("status", "")
        except Exception:
            continue
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            print(f"  [!] {keyword} @ {location}: run {status}")
            return []
    else:
        print(f"  [!] {keyword} @ {location}: poll timeout after {run_timeout}s")
        return []

    # Fetch results from dataset
    try:
        r = requests.get(
            DATASET_URL.format(dataset_id=dataset_id),
            params={"token": APIFY_API_TOKEN, "limit": limit},
            timeout=120,
        )
        return r.json() or []
    except Exception as e:
        print(f"  [!] {keyword} @ {location}: dataset fetch error: {e}")
        return []


def filter_items(items, existing_job_ids, min_days, max_days):
    """Apply date-age + dedup + basic quality filters. Returns (rows, stats_dict)."""
    rows = []
    stats = {"total": len(items), "no_company": 0, "wrong_age": 0,
             "dupe_existing": 0, "kept": 0}
    today = date.today()

    for item in items:
        job_id = str(item.get("id") or "").strip()
        if job_id and job_id in existing_job_ids:
            stats["dupe_existing"] += 1
            continue

        company_name = (item.get("companyName") or "").strip()
        if not company_name:
            stats["no_company"] += 1
            continue

        posted = parse_iso_date(item.get("publishedAt") or "")
        if posted is None:
            stats["wrong_age"] += 1
            continue
        days_old = (today - posted).days
        if days_old < min_days or days_old > max_days:
            stats["wrong_age"] += 1
            continue

        rows.append((job_id, map_to_row(item)))
        stats["kept"] += 1

    return rows, stats


def load_existing_job_ids(service, sheet_id):
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A2:A50000"
        ).execute()
    except Exception as e:
        print(f"  [!] Could not read existing Job_Ids: {e}")
        return set()
    return {r[0] for r in resp.get("values", []) if r and r[0]}


def append_batch(service, sheet_id, batch, max_retries=6):
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
                print(f"  [!] Sheets {status} — backing off {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 64)
                continue
            raise


def ensure_headers(service, sheet_id):
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
    parser = argparse.ArgumentParser(description="Scrape LinkedIn HR specialist roles → Google Sheet")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=50, help="Per-combo item cap (default 50)")
    parser.add_argument("--min_days", type=int, default=30, help="Minimum posting age in days (default 30)")
    parser.add_argument("--max_days", type=int, default=45, help="Maximum posting age in days (default 45)")
    parser.add_argument("--keywords", default="", help="Comma-separated override")
    parser.add_argument("--locations", default="", help="Comma-separated override")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent Apify runs (default 5)")
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        sys.exit(1)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else DEFAULT_KEYWORDS
    locations = [l.strip() for l in args.locations.split(",") if l.strip()] if args.locations else DEFAULT_LOCATIONS

    if not keywords or not locations:
        print("ERROR: --keywords and --locations are required. Example:")
        print('  --keywords "Benefits Manager" --locations "California"')
        sys.exit(1)
    limit = max(1, args.limit)

    combos = [(k, l) for k in keywords for l in locations]
    posted_after = (date.today() - timedelta(days=args.max_days)).isoformat()

    print("=== LinkedIn HR Specialist Scrape Orchestrator ===")
    print(f"Actor:      {ACTOR_ID}")
    print(f"Keywords:   {len(keywords)}  {keywords}")
    print(f"Locations:  {len(locations)}  {locations}")
    print(f"Combos:     {len(combos)}")
    print(f"Limit:      {limit} per combo  (max items = {len(combos) * limit:,})")
    print(f"Workers:    {args.workers}")
    print(f"Age range:  {args.min_days}–{args.max_days} days old")
    print(f"Posted after: {posted_after}")
    print(f"Sheet:      {args.sheet_url}\n")

    if args.dry_run:
        print("[DRY RUN] No Apify calls made.")
        return

    if not args.yes:
        reply = input(f"Fire {len(combos)} actor runs? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    ensure_headers(service, sheet_id)

    print("Loading existing Job_Ids...")
    existing_job_ids = load_existing_job_ids(service, sheet_id)
    print(f"  {len(existing_job_ids):,} already in sheet.\n")

    seen = set(existing_job_ids)
    pending_batch = []
    totals = {"runs_ok": 0, "runs_fail": 0, "raw": 0, "no_company": 0,
              "wrong_age": 0, "dupe_existing": 0, "dupe_session": 0, "written": 0}
    t0 = time.time()

    def work(kw, loc):
        items = run_actor(kw, loc, limit, args.min_days, args.max_days)
        rows, stats = filter_items(items, existing_job_ids, args.min_days, args.max_days)
        return kw, loc, items, rows, stats

    print(f"Launching {len(combos)} runs with {args.workers} workers...\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, kw, loc): (kw, loc) for kw, loc in combos}
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            kw, loc = futures[fut]
            try:
                kw_r, loc_r, items, rows, stats = fut.result()
            except Exception as e:
                print(f"  [{done_count}/{len(combos)}] {kw} @ {loc}: EXC {e}")
                totals["runs_fail"] += 1
                continue

            if not items and stats["total"] == 0:
                totals["runs_fail"] += 1
            else:
                totals["runs_ok"] += 1

            totals["raw"] += stats["total"]
            totals["no_company"] += stats["no_company"]
            totals["wrong_age"] += stats["wrong_age"]
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
            print(f"  [{done_count}/{len(combos)}] {kw:30s} @ {loc:18s}  "
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
    print(f"Runs ok / fail:      {totals['runs_ok']} / {totals['runs_fail']}")
    print(f"Raw items:           {totals['raw']:,}")
    print(f"  Skipped no company:   {totals['no_company']:,}")
    print(f"  Skipped wrong age:    {totals['wrong_age']:,}  (outside {args.min_days}–{args.max_days} day window)")
    print(f"  Skipped dupe existing:{totals['dupe_existing']:,}")
    print(f"  Skipped dupe session: {totals['dupe_session']:,}")
    print(f"Rows written:        {totals['written']:,}")
    print(f"Elapsed:             {elapsed}s")
    print(f"Sheet:               {args.sheet_url}")

    if totals["written"] == 0 and totals["wrong_age"] > 0 and totals["raw"] > 0:
        print(
            "\n[!] WARNING: All raw items filtered by age window. "
            "The actor may not be honouring the postedAfter parameter. "
            "Check publishedAt values in raw output."
        )


if __name__ == "__main__":
    main()
