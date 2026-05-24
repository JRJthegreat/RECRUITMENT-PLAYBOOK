"""
Reingest from existing Apify actor runs — no employee cap, no date filter.

Lists the last N runs of valig/indeed-jobs-scraper for this account, fetches
their stored dataset items, and writes everything (deduplicated by Job_Id) to
the Google Sheet. Use this to replay a scrape with looser filters without
spending Apify credits on new actor runs.

Usage:
  python3 -W ignore reingest_from_apify.py --sheet_url "URL" [--run_count 88] [--hours 2]
"""

import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

import requests
from googleapiclient.errors import HttpError

from pull_dataset import (
    TAB_NAME, APIFY_API_TOKEN, BATCH_SIZE,
    get_sheet_id_from_url, get_google_service, map_to_row,
)

ACTOR_ID = "valig~indeed-jobs-scraper"
APIFY_BASE = "https://api.apify.com/v2"


def list_recent_runs(hours=None, run_count=None):
    resp = requests.get(
        f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
        params={"token": APIFY_API_TOKEN, "limit": 200, "desc": "1"},
        timeout=30,
    )
    resp.raise_for_status()
    runs = resp.json().get("data", {}).get("items", [])

    if run_count:
        return runs[:run_count]

    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        filtered = []
        for run in runs:
            started = run.get("startedAt", "")
            try:
                dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                if dt >= cutoff:
                    filtered.append(run)
            except Exception:
                pass
        return filtered

    return runs


def fetch_dataset(dataset_id, limit=1000):
    resp = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        params={"token": APIFY_API_TOKEN, "limit": limit, "clean": "true"},
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        return []
    try:
        return resp.json() or []
    except ValueError:
        return []


def clear_sheet_data(service, sheet_id):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_sheet_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == TAB_NAME:
            tab_sheet_id = s["properties"]["sheetId"]
            break
    if tab_sheet_id is None:
        raise RuntimeError(f"Tab {TAB_NAME!r} not found")

    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_NAME}'!A2:A50000"
    ).execute()
    existing = resp.get("values", [])
    if not existing:
        print("Sheet already empty.")
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"deleteDimension": {"range": {
            "sheetId": tab_sheet_id, "dimension": "ROWS",
            "startIndex": 1, "endIndex": 1 + len(existing),
        }}}]},
    ).execute()
    print(f"Cleared {len(existing)} existing rows.")


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


def main():
    parser = argparse.ArgumentParser(description="Reingest healthcare leads from stored Apify datasets")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--run_count", type=int, default=88, help="Last N actor runs to fetch (default 88)")
    parser.add_argument("--hours", type=int, default=0, help="Alternative: fetch runs started in last N hours")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        sys.exit(1)

    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service()

    print("=== Reingest from Apify Datasets (no employee cap, no date filter) ===")
    print(f"Sheet: {sheet_id}\n")

    print("Listing recent Apify runs...")
    runs = list_recent_runs(
        hours=args.hours if args.hours else None,
        run_count=args.run_count if not args.hours else None,
    )
    print(f"Found {len(runs)} runs to pull from.\n")

    if not runs:
        print("No runs found. Try --hours 2 instead.")
        sys.exit(1)

    all_items = []
    for i, run in enumerate(runs, 1):
        ds_id = run.get("defaultDatasetId", "")
        status = run.get("status", "")
        if not ds_id or status not in ("SUCCEEDED", "RUNNING"):
            continue
        items = fetch_dataset(ds_id, limit=1000)
        all_items.extend(items)
        if i % 10 == 0 or i == len(runs):
            print(f"  Fetched {i}/{len(runs)} datasets → {len(all_items):,} items")

    print(f"\nTotal raw items from Apify: {len(all_items):,}")

    seen = set()
    rows_to_write = []
    skipped_no_company = 0
    skipped_dupe = 0

    for item in all_items:
        job_id = item.get("key") or ""
        emp = item.get("employer") or {}
        company_name = (emp.get("name") or "").strip()
        if not company_name:
            skipped_no_company += 1
            continue
        if job_id and job_id in seen:
            skipped_dupe += 1
            continue
        seen.add(job_id)
        rows_to_write.append(map_to_row(item))

    print(f"Skipped (no company): {skipped_no_company:,}")
    print(f"Skipped (duplicate):  {skipped_dupe:,}")
    print(f"Rows to write:        {len(rows_to_write):,}\n")

    print("Clearing existing sheet data...")
    clear_sheet_data(service, sheet_id)

    written = 0
    pending = list(rows_to_write)
    while pending:
        chunk = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]
        append_batch(service, sheet_id, chunk)
        written += len(chunk)
        print(f"  Written {written}/{len(rows_to_write)}")
        time.sleep(1.2)

    print(f"\nDone. {written} rows written.")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
