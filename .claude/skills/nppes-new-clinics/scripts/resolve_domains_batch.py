"""
Domain resolution for the commercial sheet WITH a failure marker.

Why this exists (2026-08-04): find_company_domains.py leaves failed lookups
blank with no record of the attempt, and always takes the FIRST N pending
companies in sheet order — so consecutive runs re-Google the same failures
(400 lookups -> 7 new domains on the second pass). That script feeds the
LIVE Indiana pipeline and must not be edited, so this wrapper imports its
battle-tested functions (same Apify Google search, same LLM
NONE-on-collision pick) and adds exactly one behavior: misses get
AB='fcd_no_match' and are skipped forever after.

Row selection: company (K) non-empty, website (L) blank, AB blank.
Found -> L=domain. Miss -> AB='fcd_no_match'. Batch-of-10 writes.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/resolve_domains_batch.py \
      --sheet_url URL --limit 400 [--dry_run]
"""
import argparse
import importlib.util
import json
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FCD_PATH = os.path.join(SCRIPT_DIR, "..", "..", "healthcare-demand-pipeline",
                        "scripts", "find_company_domains.py")
spec = importlib.util.spec_from_file_location("fcd", FCD_PATH)
fcd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fcd)          # proven logic, imported not copied

TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
TAB = "Leads"
C_COMPANY, C_WEBSITE, C_AB = 10, 11, 27   # K, L, AB


def get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(token=td["token"], refresh_token=td["refresh_token"],
                        token_uri=td["token_uri"], client_id=td["client_id"],
                        client_secret=td["client_secret"],
                        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]))
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:AB").execute().get("values", [])

    def cell(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""

    by_company = {}
    for n, r in enumerate(values, start=2):
        comp = cell(r, C_COMPANY)
        if comp and not cell(r, C_WEBSITE) and not cell(r, C_AB):
            by_company.setdefault(comp, []).append(n)
    companies = list(by_company.keys())
    if args.limit:
        companies = companies[:args.limit]
    print(f"[fcd-wrap] {len(companies)} unique fresh companies "
          f"(~${len(companies)*0.007:.2f} Apify){' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run or not companies:
        return

    found = 0
    for b in range(0, len(companies), 10):
        chunk = companies[b:b + 10]
        queries = [fcd.build_query(c) for c in chunk]
        results = fcd.apify_google_search(queries)
        updates = []
        for comp, q in zip(chunk, queries):
            organic = results.get(q, [])
            domain = fcd.pick_domain_from_results(organic, comp) if organic else ""
            for n in by_company[comp]:
                if domain:
                    updates.append({"range": f"'{TAB}'!L{n}", "values": [[domain]]})
                else:
                    updates.append({"range": f"'{TAB}'!AB{n}", "values": [["fcd_no_match"]]})
            found += bool(domain)
        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
        print(f"  {min(b+10, len(companies))}/{len(companies)} | resolved {found}",
              flush=True)
        time.sleep(0.5)
    print(f"[fcd-wrap] done: {found}/{len(companies)} resolved")


if __name__ == "__main__":
    main()
