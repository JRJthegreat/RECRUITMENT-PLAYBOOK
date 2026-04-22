"""
Phase 2.5: Verify DMs actually work at the target company.

find_dm.py uses Google snippet + regex checks which can't distinguish current
from past employment. Confirmed false positives (e.g. "Genus Facilities Mgmt"
matched MD of "Genus Recycling"). This script calls the Apify actor
`dev_fusion/Linkedin-Profile-Scraper` on every LinkedIn URL in col V, reads
the profile's CURRENT companyName + companyWebsite, and compares against the
target row's company name (K) and website (L).

Mismatches get col T/U/V cleared so find_dm.py can re-query them.

Dry-run by default. Re-run with --apply to clear mismatches.
"""

import os
import re
import json
import argparse
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
ACTOR_ID = "dev_fusion~Linkedin-Profile-Scraper"
SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

TAB_NAME = "Leads"
# Actual sheet layout (differs from legacy SKILL.md spec):
COL_COMPANY_WEBSITE = 10  # K
COL_DM_NAME = 18          # S
COL_COMPANY_NAME = 19     # T
COL_DM_TITLE = 20         # U
COL_LINKEDIN = 21         # V

BATCH_SIZE = 20         # profile URLs per actor call
WORKERS = 4             # concurrent actor runs


STOPWORDS = {
    "ltd", "limited", "llp", "plc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "group", "holdings", "services", "solutions", "consulting",
    "consultants", "the", "and", "of", "uk", "gb", "global", "international",
    "intl", "pvt", "private", "sa", "bv", "nv", "gmbh", "srl", "ab",
}


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"], client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def normalize_domain(url_or_domain):
    if not url_or_domain:
        return ""
    s = url_or_domain.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0]
    s = s.lstrip("www.")
    return s


def domain_root(domain):
    """Take 'www.foo.co.uk' -> 'foo'."""
    d = normalize_domain(domain)
    if not d:
        return ""
    parts = d.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def tokenize_company(name):
    if not name:
        return set()
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    toks = {t for t in s.split() if t and t not in STOPWORDS and len(t) > 1}
    return toks


def run_actor(profile_urls, timeout=300):
    try:
        resp = requests.post(
            SYNC_URL,
            params={"token": APIFY_API_TOKEN},
            json={"profileUrls": profile_urls},
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"  [!] Actor request failed: {type(e).__name__}: {e}")
        return []
    if resp.status_code not in (200, 201):
        print(f"  [!] Actor HTTP {resp.status_code}: {resp.text[:200]}")
        return []
    try:
        return resp.json() or []
    except ValueError:
        return []


def squish(s):
    """lower, strip everything non-alphanumeric, drop stopwords as whole words first."""
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    toks = [t for t in s.split() if t and t not in STOPWORDS]
    return "".join(toks)


def match_employer(target_company, target_domain, scraped_company, scraped_website):
    """Return (is_match, match_type, detail)."""
    tgt_dom_root = domain_root(target_domain)
    scr_dom_root = domain_root(scraped_website)
    if tgt_dom_root and scr_dom_root and tgt_dom_root == scr_dom_root:
        return True, "domain", f"{tgt_dom_root}"

    # Squished name comparison — handles "Yu Group PLC" vs "yugroupplc.com"
    tgt_squish = squish(target_company)
    scr_squish = squish(scraped_company)
    if tgt_squish and scr_squish:
        if tgt_squish == scr_squish:
            return True, "squish-eq", tgt_squish
        if len(tgt_squish) >= 5 and tgt_squish in scr_squish:
            return True, "squish-contain", f"{tgt_squish!r} in {scr_squish!r}"
        if len(scr_squish) >= 5 and scr_squish in tgt_squish:
            return True, "squish-contain", f"{scr_squish!r} in {tgt_squish!r}"

    # Also try target domain-root against scraped squished name
    if tgt_dom_root and scr_squish and len(tgt_dom_root) >= 5:
        if tgt_dom_root in scr_squish or scr_squish in tgt_dom_root:
            return True, "domain-in-name", f"{tgt_dom_root} ~ {scr_squish}"

    tgt_tokens = tokenize_company(target_company)
    scr_tokens = tokenize_company(scraped_company)
    if not tgt_tokens or not scr_tokens:
        return False, "no-tokens", f"tgt={target_company!r} scr={scraped_company!r}"

    overlap = tgt_tokens & scr_tokens
    # Require at least one distinctive shared token AND the shared tokens cover
    # at least half of the target's distinctive tokens (guards against
    # "Genus Facilities Mgmt" matching "Genus Recycling" via single "genus").
    if not overlap:
        return False, "no-overlap", f"tgt={sorted(tgt_tokens)} scr={sorted(scr_tokens)}"
    if len(overlap) * 2 < len(tgt_tokens):
        return False, "weak-overlap", f"shared={sorted(overlap)} need≥{(len(tgt_tokens)+1)//2}"
    return True, "name", f"shared={sorted(overlap)}"


def main():
    parser = argparse.ArgumentParser(description="Verify DMs work at target company via LinkedIn profile scrape")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--apply", action="store_true", help="Clear T/U/V for mismatches")
    parser.add_argument("--limit", type=int, default=0, help="Only verify first N rows (debug)")
    args = parser.parse_args()

    if not APIFY_API_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set")
        return

    spreadsheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_service()

    mode = "APPLY (clear mismatches)" if args.apply else "DRY RUN"
    print(f"=== Verify DMs ({mode}) ===")
    print(f"Actor: {ACTOR_ID}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{TAB_NAME}!A2:V10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    work = []
    for i, r in enumerate(rows):
        sheet_row = i + 2
        company = r[COL_COMPANY_NAME] if len(r) > COL_COMPANY_NAME else ""
        website = r[COL_COMPANY_WEBSITE] if len(r) > COL_COMPANY_WEBSITE else ""
        dm_name = r[COL_DM_NAME] if len(r) > COL_DM_NAME else ""
        dm_title = r[COL_DM_TITLE] if len(r) > COL_DM_TITLE else ""
        linkedin = r[COL_LINKEDIN] if len(r) > COL_LINKEDIN else ""
        if not linkedin.strip() or not linkedin.startswith("http"):
            continue
        work.append((sheet_row, company, website, dm_name, dm_title, linkedin.strip()))

    if args.limit:
        work = work[:args.limit]
    print(f"DMs to verify: {len(work)}\n")
    if not work:
        return

    # URL -> (sheet_row, company, website, dm_name, dm_title)
    meta_by_url = {}
    for sr, c, w, n, t, u in work:
        meta_by_url[u] = (sr, c, w, n, t)

    urls = list(meta_by_url.keys())
    batches = [urls[i:i + BATCH_SIZE] for i in range(0, len(urls), BATCH_SIZE)]
    print(f"Running {len(batches)} actor calls ({BATCH_SIZE}/batch, {WORKERS} workers)...\n")

    all_results = {}  # url -> item dict

    def work_batch(batch):
        return batch, run_actor(batch)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work_batch, b) for b in batches]
        done = 0
        for fut in as_completed(futures):
            batch, items = fut.result()
            done += 1
            for item in items or []:
                url = (item.get("linkedinUrl") or item.get("linkedinPublicUrl") or "").strip()
                if not url:
                    continue
                all_results[url.rstrip("/").lower()] = item
            print(f"  [{done}/{len(batches)}] batch size={len(batch)} returned={len(items or [])}")

    print(f"\nScraped profiles: {len(all_results)} / {len(urls)}\n")

    matches, mismatches, missing = [], [], []

    for sr, company, website, dm_name, dm_title, url in work:
        key = url.rstrip("/").lower()
        item = all_results.get(key)
        if not item:
            missing.append((sr, company, dm_name, url, "profile not scraped"))
            continue
        scraped_company = (item.get("companyName") or "").strip()
        scraped_website = (item.get("companyWebsite") or "").strip()
        scraped_title = (item.get("jobTitle") or "").strip()
        full_name = (item.get("fullName") or "").strip()
        is_employed = item.get("isCurrentlyEmployed")
        still_working = item.get("jobStillWorking")

        ok, mtype, detail = match_employer(company, website, scraped_company, scraped_website)
        if ok:
            matches.append((sr, company, dm_name, full_name, scraped_company, scraped_title, mtype, detail))
        else:
            mismatches.append((sr, company, dm_name, full_name, scraped_company, scraped_title, mtype, detail, is_employed, still_working))

    print(f"=== MATCH:    {len(matches)} ===")
    print(f"=== MISMATCH: {len(mismatches)} ===")
    print(f"=== MISSING:  {len(missing)} (profile not returned by actor) ===\n")

    if mismatches:
        print("Mismatches (will be cleared with --apply):")
        for sr, company, dm_name, full_name, scraped_company, scraped_title, mtype, detail, emp, still in mismatches:
            print(f"  row {sr}: target={company!r}")
            print(f"           dm={dm_name!r} -> profile={full_name!r}")
            print(f"           current: {scraped_title!r} @ {scraped_company!r}")
            print(f"           reason:  {mtype} ({detail})")
            print()

    if missing:
        print("Missing (profile not in actor output — LinkedIn may have blocked, or URL bad):")
        for sr, company, dm_name, url, reason in missing[:20]:
            print(f"  row {sr}: {company!r} / {dm_name!r} / {url}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")
        print()

    if not args.apply:
        print("[DRY RUN] No changes. Re-run with --apply to clear mismatches.")
        return

    to_clear = [sr for sr, *_ in mismatches]
    if not to_clear:
        print("Nothing to clear.")
        return

    print(f"Clearing S (DM Name), U (DM Title), V (LinkedIn) for {len(to_clear)} rows...")
    data = []
    for sr in to_clear:
        data.append({"range": f"{TAB_NAME}!S{sr}", "values": [[""]]})
        data.append({"range": f"{TAB_NAME}!U{sr}:V{sr}", "values": [["", ""]]})
    BATCH = 100
    for i in range(0, len(data), BATCH):
        chunk = data[i:i + BATCH]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": chunk},
        ).execute()
        print(f"  Cleared chunk {i // BATCH + 1}/{(len(data) + BATCH - 1) // BATCH}")

    print(f"\nCleared {len(to_clear)} DM rows. Re-run find_dm.py to re-query.")
    print("=== Done ===")


if __name__ == "__main__":
    main()
