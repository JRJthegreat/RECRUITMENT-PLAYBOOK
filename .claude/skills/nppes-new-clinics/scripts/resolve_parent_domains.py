"""
Parent-org domain pass (Jude, 2026-08-04): for rows where the site's own legal
name resolved to nothing, the Parent Org column often names who they REALLY
are — "FAIRVIEW OPCO LLC" is unfindable, its parent brand is not. And for a
health-system site the parent's domain is exactly where the buying-power
contact lives, so writing the parent domain is correct, not a compromise.

Reuses the battle-tested resolution logic from
healthcare-demand-pipeline/scripts/find_company_domains.py VERBATIM by import
(same Apify Google search, same blocked-host filter, same LLM NONE-on-collision
pick) — nothing about the proven flow is reimplemented here. The only change
is WHICH name gets searched: the parent LBN instead of the shell LLC.

Targets rows on the commercial sheet where:
  website (L) is blank  AND  Parent Org (J) is non-empty

Writes the resolved domain to L with AB = 'parent_domain'.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/resolve_parent_domains.py \
      --sheet_url URL [--limit N] [--dry_run]
"""
import argparse
import importlib.util
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FCD_PATH = os.path.join(SCRIPT_DIR, "..", "..", "healthcare-demand-pipeline",
                        "scripts", "find_company_domains.py")
spec = importlib.util.spec_from_file_location("fcd", FCD_PATH)
fcd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fcd)          # proven logic, imported not copied

TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
TAB = "Leads"
C_STATUS_LEAD, C_PARENT, C_COMPANY, C_WEBSITE = 4, 9, 10, 11   # E, J, K, L


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
        spreadsheetId=sid, range=f"'{TAB}'!A2:AI").execute().get("values", [])

    def cell(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""

    todo = [(n, cell(r, C_PARENT), cell(r, C_COMPANY))
            for n, r in enumerate(values, start=2)
            if not cell(r, C_WEBSITE) and cell(r, C_PARENT)]
    if args.limit:
        todo = todo[:args.limit]
    # one lookup per distinct parent — a system with 40 sites is ONE search
    by_parent = {}
    for n, parent, comp in todo:
        by_parent.setdefault(parent, []).append(n)
    print(f"[parent] {len(todo)} rows lack a domain but name a parent org "
          f"({len(by_parent)} distinct parents)"
          f"{' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run:
        for p, rows in list(by_parent.items())[:15]:
            print(f"   {len(rows):3d} rows <- {p[:60]}")
        return

    resolved, updates = 0, []
    parents = list(by_parent.items())
    for i in range(0, len(parents), 10):
        batch = parents[i:i + 10]
        queries = [fcd.build_query(p) for p, _ in batch]
        results = fcd.apify_google_search(queries)
        for (parent, rows), q in zip(batch, queries):
            organic = results.get(q, [])
            domain = fcd.pick_domain_from_results(organic, parent) if organic else ""
            if not domain:
                continue
            resolved += 1
            for n in rows:
                updates.append({"range": f"'{TAB}'!L{n}", "values": [[domain]]})
                updates.append({"range": f"'{TAB}'!AB{n}", "values": [["parent_domain"]]})
        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
            updates = []
        print(f"  parents searched {min(i+10, len(parents))}/{len(parents)} "
              f"| resolved {resolved}", end="\r")
        time.sleep(0.5)
    print(f"\n[parent] {resolved}/{len(parents)} parents resolved -> domains "
          f"written to their sites' rows")


if __name__ == "__main__":
    main()
