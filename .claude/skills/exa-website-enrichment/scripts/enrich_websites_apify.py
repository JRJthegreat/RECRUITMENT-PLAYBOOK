"""
Resolve company websites via Apify's google-search-scraper instead of Exa —
same candidate-collection / Claude-judges-in-session / apply-verdicts flow as
enrich_websites_exa.py, different search backend.

WHY THIS EXISTS
---------------
Jude's call (2026-08-10): Exa ran out of credits mid-campaign (production
lane, batch 4). Rather than wait on a top-up, use Apify — already paid for,
already the source for healthcare-demand-pipeline/find_company_domains.py,
Jude's own tested resolver. This script mirrors that proven script's search
layer (same actor, same batched-query call, same query format) rather than
inventing a new one.

WHAT'S DIFFERENT FROM find_company_domains.py
-----------------------------------------------
find_company_domains.py judges with an LLM API call and verifies with a
content-overlap heuristic. This script does neither: it fetches each
surviving candidate's actual page text (like that script's verify() does)
and hands domain + title + page text to CLAUDE IN THE SESSION to judge —
same as enrich_websites_exa.py's flow, same mechanical guards (junk hosts,
careers/job pages, parent-chain), same candidates.json / verdicts.json
shape. The apply step is UNCHANGED — this script only replaces collection.

Status stamps use the same "exa_" prefix as the Exa collector (it denotes
"a website-resolution attempt happened", not which engine ran it) so a row
resolved by one backend is correctly skipped by the other on a later run.

COST
----
Apify google-search-scraper is usage-based; check actual spend in the Apify
Console after a run. This script does not print a dollar estimate — unlike
Exa's flat per-search cost, printing a guess here would be more likely to
mislead than help.

USAGE
-----
  # step 1 — collect candidates (Apify Google Search + page-content fetch)
  python3 -W ignore enrich_websites_apify.py --sheet_url "URL" --limit 20          # dry run
  python3 -W ignore enrich_websites_apify.py --sheet_url "URL" --apply \
      --niche "video production company" --candidates candidates.json

  # step 2 — Claude judges candidates.json in-session -> verdicts.json

  # step 3 — apply verdicts (same script as the Exa path; source-agnostic)
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" \
      --candidates candidates.json --verdicts verdicts.json --apply
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
# Reuse the mechanical filters and column helpers verbatim rather than
# duplicating them — same junk-host list, same careers/job-page rejection,
# same parent-chain guard, same col-letter math as the Exa collector.
import enrich_websites_exa as base  # noqa: E402

load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_BASE = "https://api.apify.com/v2"
APIFY_ACTOR = "apify~google-search-scraper"
QUERY_BATCH = 10       # queries per Apify call — matches find_company_domains.py
FETCH_WORKERS = 8      # parallel page-content fetches
MAX_FETCH_PER_ROW = 3  # only fetch content for the top N surviving candidates

HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

LEGAL_SUFFIX_RE = re.compile(r"\s+(ltd|limited|llc|inc|corp|corporation|co)\.?$", re.I)


def build_query(company, niche):
    """Mirrors find_company_domains.py's build_query: strip trailing legal
    suffixes (they trigger business-registry results), quote the name."""
    cleaned = LEGAL_SUFFIX_RE.sub("", company.strip()).strip()
    parts = [f'"{cleaned}"']
    if niche:
        parts.append(niche)
    parts.append("official website")
    return " ".join(parts)


def apify_google_search(queries, country_code):
    """Batched Google search via Apify. Returns {query: [organicResults]}.

    Retries transient network failures (DNS hiccups, connection resets) with
    backoff — an earlier version let a bare ConnectionError crash the whole
    run on batch 1 with zero rows saved, because candidates.json is only
    written after the full loop finishes. Retrying here is cheaper than
    losing an entire 300-row run to one blip."""
    for attempt in range(4):
        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json={
                    "queries": "\n".join(queries),
                    "resultsPerPage": 6,
                    "maxPagesPerQuery": 1,
                    "languageCode": "en",
                    "countryCode": country_code,
                    "includeUnfilteredResults": False,
                },
                timeout=300,
            )
        except requests.RequestException as e:
            if attempt == 3:
                print(f"  [!] network error after retries: {type(e).__name__}: {e}")
                return {}
            wait = 2 ** attempt * 3
            print(f"  [!] {type(e).__name__}, retrying in {wait}s ({attempt + 1}/4)")
            time.sleep(wait)
            continue
        if resp.status_code not in (200, 201):
            print(f"  [!] Apify HTTP {resp.status_code}: {resp.text[:200]}")
            return {}
        out = {}
        for item in resp.json():
            q = item.get("searchQuery", {}).get("term", "")
            if q:
                out[q] = item.get("organicResults", [])
        return out
    return {}


def fetch_page_text(url, max_chars=1200):
    """Real page content for a candidate — the same evidence class Exa's
    /search+contents call provides, fetched directly since Apify's search
    actor only returns title+snippet. Falls back to '' on any failure; the
    candidate still carries title+snippet either way."""
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=8, allow_redirects=True)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text[:60000], "html.parser")
        text = soup.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text)[:max_chars]
    except Exception:
        return ""


def build_candidates_apify(company, organic, taken, country_code):
    """Mechanical prefilter (reusing base.is_junk / base.looks_like_job_page /
    base.norm_domain / the parent-chain guard) then fetch page content for
    the survivors. Mirrors base.build_candidates()'s guard logic exactly."""
    prelim, seen = [], set()
    for r in organic[:10]:
        url = r.get("url", "")
        dom = base.norm_domain(url)
        if not dom or "." not in dom or dom in seen:
            continue
        if base.is_junk(dom):
            continue
        title = r.get("title", "") or ""
        desc = r.get("description", "") or ""
        if base.looks_like_job_page(url, title + " " + desc):
            continue
        owner = taken.get(dom)
        if owner and owner.lower() != (company or "").lower():
            continue
        seen.add(dom)
        prelim.append({"domain": dom, "url": url, "title": title[:150], "snippet": desc[:300]})

    to_fetch = prelim[:MAX_FETCH_PER_ROW]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        texts = list(pool.map(lambda c: fetch_page_text(c["url"]), to_fetch))

    candidates = []
    for c, text in zip(to_fetch, texts):
        candidates.append({"domain": c["domain"], "title": c["title"],
                           "text": text or c["snippet"]})
    # Candidates past MAX_FETCH_PER_ROW keep only their snippet (no fetch) —
    # weaker evidence, but still judgeable and better than dropping them.
    for c in prelim[MAX_FETCH_PER_ROW:]:
        candidates.append({"domain": c["domain"], "title": c["title"], "text": c["snippet"]})
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--col_company", default="K")
    ap.add_argument("--col_website", default="L")
    ap.add_argument("--col_city", default="R")
    ap.add_argument("--col_state", default="S")
    ap.add_argument("--col_status", default="")
    ap.add_argument("--niche", default="", help='e.g. "video production company"')
    ap.add_argument("--country_code", default="us")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--retry_attempted", action="store_true")
    ap.add_argument("--candidates", default="apify_candidates.json")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not APIFY_TOKEN:
        sys.exit("APIFY_API_TOKEN not set. Add it to .claude/.env")

    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
    svc = build("sheets", "v4", credentials=Credentials.from_authorized_user_file(TOKEN_PATH))
    vals = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{args.tab}!A1:BZ20000").execute().get("values", [])
    rows = vals[1:]
    C, W = base.col_idx(args.col_company), base.col_idx(args.col_website)
    CY, ST = base.col_idx(args.col_city), base.col_idx(args.col_state)
    STA = base.col_idx(args.col_status) if args.col_status else None

    def cell(r, i):
        return (r[i] if i is not None and len(r) > i else "").strip()

    taken = {}
    for r in rows:
        d = base.norm_domain(cell(r, W))
        if d and "." in d:
            taken.setdefault(d, cell(r, C))

    todo = []
    for i, r in enumerate(rows):
        name = cell(r, C)
        if not name:
            continue
        if cell(r, W) and "." in cell(r, W) and not args.overwrite:
            continue
        # Same "exa_" prefix as the Exa collector — see module docstring.
        if (STA is not None and cell(r, STA).startswith("exa_")
                and not args.overwrite and not args.retry_attempted):
            continue
        todo.append({"row": i + 2, "company": name,
                     "city": cell(r, CY), "state": cell(r, ST)})
    if args.limit:
        todo = todo[:args.limit]

    print(f"=== Apify candidate collection {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
    print(f"rows needing a website: {len(todo)}")
    if not args.apply:
        print("\n[DRY RUN] no Apify calls, nothing spent, nothing written. Re-run with --apply.")
        for t in todo[:15]:
            print(f"  would resolve: {t['company']}  ({t['city']}, {t['state']})")
        return

    errors_out = []
    collected_out = []
    num_batches = (len(todo) + QUERY_BATCH - 1) // QUERY_BATCH

    def checkpoint():
        # Write whatever has been collected so far. Called after every batch
        # AND from the except/finally paths below — after batch 5 crashed
        # cold on a DNS blip with zero rows saved (candidates.json was only
        # ever written once, at the very end), losing an in-progress run to
        # any single failure stopped being acceptable.
        out = sorted(collected_out, key=lambda c: c["row"])
        with open(args.candidates, "w") as f:
            json.dump(out, f, indent=1)

    try:
        for b in range(num_batches):
            chunk = todo[b * QUERY_BATCH:(b + 1) * QUERY_BATCH]
            queries = [build_query(t["company"], args.niche) for t in chunk]
            q_to_t = dict(zip(queries, chunk))
            print(f"batch {b + 1}/{num_batches} ({len(chunk)} companies)")
            results = apify_google_search(queries, args.country_code)
            if not results and not any(q in results for q in queries):
                # Whole batch came back empty — surface it loudly rather than
                # silently marking every row no_match (mirrors the lesson
                # from the Exa collector's swallowed-exception bug: a backend
                # outage must never look identical to "no website found").
                print(f"  [!] batch {b + 1} returned nothing from Apify — "
                      f"skipping this batch, NOT stamping these rows as attempted")
                continue
            for q, t in q_to_t.items():
                cands = build_candidates_apify(t["company"], results.get(q, []), taken, args.country_code)
                if not cands:
                    errors_out.append((t, "no_match"))
                else:
                    collected_out.append({**t, "candidates": cands})
            checkpoint()
            time.sleep(1.0)
    except Exception as e:
        checkpoint()
        print(f"\n!!! Run interrupted by {type(e).__name__}: {e}")
        print(f"    {len(collected_out)} rows already collected and saved to "
              f"{args.candidates} — safe to judge those now, then re-run this "
              f"exact command (nothing un-stamped was lost) to pick up the rest.")
        raise

    if STA is not None and errors_out:
        writes = [{"range": f"{args.tab}!{base.idx_col(STA)}{t['row']}",
                   "values": [[f"exa_{err}"]]} for t, err in errors_out]
        for k in range(0, len(writes), 100):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid, body={"valueInputOption": "RAW",
                                         "data": writes[k:k + 100]}).execute()

    collected_out.sort(key=lambda c: c["row"])
    with open(args.candidates, "w") as f:
        json.dump(collected_out, f, indent=1)

    print(f"\n=== Summary ===")
    print(f"  collected:      {len(collected_out)}")
    print(f"  no_candidates:  {len(errors_out)}")
    print(f"\n{len(collected_out)} rows with candidates -> {args.candidates}")
    print("Next: have Claude read that file, write verdicts JSON, then apply with "
          "enrich_websites_exa.py --candidates ... --verdicts ... --apply")


if __name__ == "__main__":
    main()
