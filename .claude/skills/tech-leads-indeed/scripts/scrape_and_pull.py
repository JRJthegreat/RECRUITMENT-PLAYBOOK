"""
Phase 1: Orchestrate Apify Indeed scrapes (pan-EU tech) → Google Sheet.

Iterates keyword × city grid across European tech hubs, calling the
`valig/indeed-jobs-scraper` actor once per combo. Filters at ingestion
(≤ MAX_EMPLOYEES, datePublished within last N days, no duplicate Job_Ids).
Streams rows into the sheet in batches of 10 as combos complete.

Usage:
  python3 -W ignore scrape_and_pull.py --sheet_url "URL" [options]

  --limit 100                 per-combo item cap (actor max is 1000)
  --days 14                   datePosted filter (Indeed supports 1/3/7/14)
  --cities "London:gb,..."    comma-separated override ("City:cc" pairs)
  --keywords "A,B,..."        comma-separated override
  --workers 8                 concurrent Apify runs
  --dry_run                   print plan only, no Apify calls
  --yes                       skip confirmation prompt

Resume safety: existing Job_Ids in the sheet are loaded first; matching items
returned by the actor are skipped (no duplicate rows).

No jobtype filter — Perm, Contract and Freelance rows all flow through a
single template downstream.
"""

import sys
import time
import argparse
import requests
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from pull_dataset import (
    HEADERS, TAB_NAME, MAX_EMPLOYEES, BATCH_SIZE,
    APIFY_API_TOKEN,
    get_sheet_id_from_url, get_google_service,
    parse_size_lower_bound, map_to_row,
)

ACTOR_ID = "valig~indeed-jobs-scraper"
SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

# Pan-European tech hiring keywords. Each actor run takes ONE keyword.
# Spread covers core IC roles + AI/ML specialties + fintech-flavoured titles.
# C-level (CTO, Head of Eng) and Eng Manager removed — those are exec-search,
# not Indeed-search at ≤500 emp firms.
DEFAULT_KEYWORDS = [
    # Core IC engineering
    "Backend Engineer",
    "Frontend Engineer",
    "Full Stack Engineer",
    "DevOps Engineer",
    "Data Engineer",
    "Site Reliability Engineer",
    "Mobile Engineer",
    "iOS Engineer",
    "Android Engineer",
    "QA Engineer",
    "Platform Engineer",
    "Cloud Engineer",
    "Security Engineer",
    "Embedded Engineer",
    "Staff Engineer",
    "Solutions Engineer",
    # AI / ML
    "Machine Learning Engineer",
    "AI Engineer",
    "LLM Engineer",
    "Applied AI Engineer",
    "Generative AI Engineer",
    "Computer Vision Engineer",
    "NLP Engineer",
    "MLOps Engineer",
    "Data Scientist",
    # Fintech / specialty
    "Payments Engineer",
    "Quant Developer",
    "Blockchain Engineer",
    "Trading Systems Engineer",
    # Product (we flipped the AI filter to KEEP — tech firms hire PMs)
    "Product Manager",
]

# (city, Indeed country code) pairs. Each keyword × city run pins the
# domain so Indeed returns local results. Country codes follow ISO-3166-1
# alpha-2 lowercase (Apify actor expects them that way).
#
# Order matters: UK cities first (highest yield given language overlap with
# proof companies), then continental EU. Resume-safe duplicates dropped at
# ingest, so cities can be commented in/out without breaking re-runs.
DEFAULT_CITIES = [
    # UK priority block
    ("London", "uk"),
    ("Manchester", "uk"),
    ("Birmingham", "uk"),
    ("Leeds", "uk"),
    ("Edinburgh", "uk"),
    ("Bristol", "uk"),
    ("Cambridge", "uk"),
    ("Oxford", "uk"),
    ("Glasgow", "uk"),
    ("Liverpool", "uk"),
    ("Newcastle", "uk"),
    ("Sheffield", "uk"),
    # Continental EU
    ("Dublin", "ie"),
    ("Amsterdam", "nl"),
    ("Berlin", "de"),
    ("Munich", "de"),
    ("Paris", "fr"),
    ("Madrid", "es"),
    ("Barcelona", "es"),
    ("Lisbon", "pt"),
    ("Stockholm", "se"),
    ("Copenhagen", "dk"),
    ("Zurich", "ch"),
    ("Warsaw", "pl"),
    ("Milan", "it"),
]


def parse_city_spec(spec):
    """Parse 'City:cc' or bare 'City' (defaults country to 'gb')."""
    if ":" in spec:
        city, cc = spec.split(":", 1)
        return city.strip(), cc.strip().lower()
    return spec.strip(), "uk"


def parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def run_actor(keyword, city, country, days, limit, timeout=180):
    """Fire one actor run (sync). Returns list of items or []. No retry."""
    try:
        resp = requests.post(
            SYNC_URL,
            params={"token": APIFY_API_TOKEN},
            json={
                "title": keyword,
                "location": city,
                "datePosted": str(days),
                "country": country,
                "limit": limit,
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"  [!] {keyword} @ {city} ({country}): {type(e).__name__}: {e}")
        return []

    if resp.status_code not in (200, 201):
        print(f"  [!] {keyword} @ {city} ({country}): HTTP {resp.status_code} — {resp.text[:120]}")
        return []

    try:
        return resp.json() or []
    except ValueError:
        print(f"  [!] {keyword} @ {city} ({country}): invalid JSON response")
        return []


def filter_items(items, existing_job_ids, cutoff_date):
    """Apply size + date + dedup filters. Returns (rows, stats_dict)."""
    rows = []
    stats = {"total": len(items), "no_company": 0, "too_big": 0,
             "stale": 0, "dupe_existing": 0, "kept": 0}

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

        size_lower = parse_size_lower_bound(emp.get("employeesCount", ""))
        if size_lower is not None and size_lower > MAX_EMPLOYEES:
            stats["too_big"] += 1
            continue

        pub = parse_iso_date(item.get("datePublished") or "")
        if pub is not None and pub < cutoff_date:
            stats["stale"] += 1
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


def append_batch(service, sheet_id, batch):
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{TAB_NAME}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": batch},
    ).execute()


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
    parser = argparse.ArgumentParser(description="Scrape Apify Indeed (keyword × EU city grid) → Google Sheet")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=100, help="Per-combo actor item cap (max 1000)")
    parser.add_argument("--days", type=int, default=14, choices=[1, 3, 7, 14])
    parser.add_argument("--cities", default="", help="Comma-separated 'City:cc' override")
    parser.add_argument("--keywords", default="", help="Comma-separated override")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set in .env")
        sys.exit(1)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else DEFAULT_KEYWORDS
    if args.cities:
        cities = [parse_city_spec(c) for c in args.cities.split(",") if c.strip()]
    else:
        cities = DEFAULT_CITIES
    limit = max(1, min(args.limit, 1000))
    cutoff_date = date.today() - timedelta(days=args.days)

    combos = [(k, c, cc) for k in keywords for (c, cc) in cities]

    print("=== Apify Indeed Scrape Orchestrator (Tech EU) ===")
    print(f"Actor:     {ACTOR_ID}")
    print(f"Keywords:  {len(keywords)}  {keywords}")
    print(f"Cities:    {len(cities)}  {[f'{c}:{cc}' for c, cc in cities]}")
    print(f"Combos:    {len(combos)}")
    print(f"Limit:     {limit} per combo  (max items = {len(combos) * limit:,})")
    print(f"Days:      {args.days}  (datePublished ≥ {cutoff_date})")
    print(f"Workers:   {args.workers}")
    print(f"Max size:  ≤{MAX_EMPLOYEES} employees")
    print(f"Sheet:     {args.sheet_url}\n")

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
              "too_big": 0, "stale": 0, "dupe_existing": 0, "dupe_session": 0,
              "written": 0}
    t0 = time.time()

    def work(kw, ct, cc):
        items = run_actor(kw, ct, cc, args.days, limit)
        rows, stats = filter_items(items, existing_job_ids, cutoff_date)
        return kw, ct, cc, items, rows, stats

    print(f"Launching {len(combos)} runs with {args.workers} workers...\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, kw, ct, cc): (kw, ct, cc) for kw, ct, cc in combos}
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            kw, ct, cc = futures[fut]
            try:
                kw_r, ct_r, cc_r, items, rows, stats = fut.result()
            except Exception as e:
                print(f"  [{done_count}/{len(combos)}] {kw} @ {ct} ({cc}): EXC {e}")
                totals["runs_fail"] += 1
                continue

            if not items and stats["total"] == 0:
                totals["runs_fail"] += 1
            else:
                totals["runs_ok"] += 1

            totals["raw"] += stats["total"]
            totals["no_company"] += stats["no_company"]
            totals["too_big"] += stats["too_big"]
            totals["stale"] += stats["stale"]
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
            print(f"  [{done_count}/{len(combos)}] {kw:22s} @ {ct:12s} ({cc})  "
                  f"raw={stats['total']:3d}  new={len(session_new):3d}  "
                  f"written={totals['written']:5d}  ({elapsed}s)")

    # Flush tail
    while pending_batch:
        chunk = pending_batch[:BATCH_SIZE]
        pending_batch = pending_batch[BATCH_SIZE:]
        append_batch(service, sheet_id, chunk)
        totals["written"] += len(chunk)
        time.sleep(1.2)

    elapsed = int(time.time() - t0)
    print("\n=== Summary ===")
    print(f"Runs ok / fail:   {totals['runs_ok']} / {totals['runs_fail']}")
    print(f"Raw items:        {totals['raw']:,}")
    print(f"  Skipped no company: {totals['no_company']:,}")
    print(f"  Skipped >{MAX_EMPLOYEES} emp:   {totals['too_big']:,}")
    print(f"  Skipped stale:      {totals['stale']:,}")
    print(f"  Skipped dupe existing: {totals['dupe_existing']:,}")
    print(f"  Skipped dupe session:  {totals['dupe_session']:,}")
    print(f"Rows written:     {totals['written']:,}")
    print(f"Elapsed:          {elapsed}s")
    print(f"Sheet:            {args.sheet_url}")


if __name__ == "__main__":
    main()
