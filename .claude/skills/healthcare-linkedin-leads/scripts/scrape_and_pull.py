"""
Phase 1: Orchestrate LinkedIn scrapes → Google Sheet (US healthcare clinical roles).

Iterates keyword × location grid using insight_api_labs~linkedin-jobs-scraper.
Pain signal = LOW APPLICANT COUNT (fewApplicants=True), not posting age — there
is no date-window filter. Filters at ingestion: no duplicate Job_Ids, missing
company name. Company size is NOT filtered here (not returned by the LinkedIn
jobs actor) — run enrich_company_profiles.py after ingest.

Usage:
  python3 -W ignore scrape_and_pull.py --sheet_url "URL" --locations "A,B,..." [options]

  --limit 50             per-combo item cap (default 50)
  --locations "A;B;..."  SEMICOLON-separated, REQUIRED (LinkedIn city strings contain
                         commas, e.g. "New York, NY", so locations split on ';' not ',')
  --keywords "A,B,..."   comma-separated override (default: "Nurse Practitioner")
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

DEFAULT_KEYWORDS = ["Nurse Practitioner"]   # NP-only for now
DEFAULT_LOCATIONS = []                       # Always specify via --locations


def run_actor(keyword, location, limit, poll_interval=15, run_timeout=3600):
    """Fire one actor run (async). Returns list of items or []. No retry.

    Pain signal is fewApplicants=True (low applicant count). No date window."""
    # Start the run
    try:
        resp = requests.post(
            RUN_URL,
            params={"token": APIFY_API_TOKEN},
            json={
                "jobTitle": keyword,
                "location": location,
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


def filter_items(items, existing_job_ids):
    """Apply dedup + missing-company filters. No date-age filter. Returns (rows, stats_dict)."""
    rows = []
    stats = {"total": len(items), "no_company": 0, "dupe_existing": 0, "kept": 0}

    for item in items:
        job_id = str(item.get("id") or "").strip()
        if job_id and job_id in existing_job_ids:
            stats["dupe_existing"] += 1
            continue

        company_name = (item.get("companyName") or "").strip()
        if not company_name:
            stats["no_company"] += 1
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
    parser = argparse.ArgumentParser(description="Scrape LinkedIn healthcare clinical roles → Google Sheet")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=50, help="Per-combo item cap (default 50)")
    parser.add_argument("--keywords", default="", help="Comma-separated override (default: Nurse Practitioner)")
    parser.add_argument("--locations", default="", help="Semicolon-separated, REQUIRED (commas allowed inside a location)")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent Apify runs (default 5)")
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        sys.exit(1)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else DEFAULT_KEYWORDS
    locations = [l.strip() for l in args.locations.split(";") if l.strip()] if args.locations else DEFAULT_LOCATIONS

    if not locations:
        print("ERROR: --locations is required (semicolon-separated). Example:")
        print('  --locations "New York, NY"')
        print('  --locations "New York, NY;Baltimore, MD"')
        sys.exit(1)
    limit = max(1, args.limit)

    combos = [(k, l) for k in keywords for l in locations]

    print("=== LinkedIn Healthcare Scrape Orchestrator ===")
    print(f"Actor:      {ACTOR_ID}")
    print(f"Keywords:   {len(keywords)}  {keywords}")
    print(f"Locations:  {len(locations)}  {locations}")
    print(f"Combos:     {len(combos)}")
    print(f"Limit:      {limit} per combo  (max items = {len(combos) * limit:,})")
    print(f"Workers:    {args.workers}")
    print(f"Pain signal: low applicant count (fewApplicants=True) — no date window")
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
              "dupe_existing": 0, "dupe_session": 0, "written": 0}
    t0 = time.time()

    def work(kw, loc):
        items = run_actor(kw, loc, limit)
        rows, stats = filter_items(items, existing_job_ids)
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
    print(f"  Skipped dupe existing:{totals['dupe_existing']:,}")
    print(f"  Skipped dupe session: {totals['dupe_session']:,}")
    print(f"Rows written:        {totals['written']:,}")
    print(f"Elapsed:             {elapsed}s")
    print(f"Sheet:               {args.sheet_url}")


if __name__ == "__main__":
    main()
