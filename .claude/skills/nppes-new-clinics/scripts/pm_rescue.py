"""
Second-lane email rescue via Purple Magic (ConnectorOS), for rows AnyMail
Finder could not resolve.

Rationale: AMF returned `not_found` on 65% of these practices — they registered
30-90 days ago and barely exist on the web yet, so no single provider's database
covers them. Different providers fail on different companies, so the not_found
pile is exactly where a second lane pays.

Same rules as the AMF pass: the NPPES filer is the target (they are usually the
owner), only `status == "valid"` is written, the email domain must match the
resolved website, and free mailboxes are rejected.

  /find             {firstName,lastName,domain} -> {email,status,hosted_at}
  /decision-makers  {domain} -> {best:{fullName,firstName,lastName,title,...}}
                    used only when the filer is back-office or has no name

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/pm_rescue.py \
      --sheet_url URL [--limit 50] [--target 400] [--dry_run]
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

PM_KEY = os.getenv("PURPLE_MAGIC_KEY")
PM_BASE = "https://api.connector-os.com/api/email/v2"
TAB = "Leads"

C_WEBSITE, C_DM_NAME, C_DM_TITLE = 11, 19, 20
C_EMAIL, C_FIRST, C_LAST = 22, 23, 24
C_COMPANY, C_AO_NAME, C_AO_TITLE, C_STATUS = 10, 28, 29, 31

BACK_OFFICE_RE = re.compile(
    r"\b(CFO|CHIEF FINANCIAL|VP FINANCE|FINANCE|CONTROLLER|TREASURER|SECRETARY"
    r"|CREDENTIALING|COMPLIANCE|LEGAL|COUNSEL|BILLING|REVENUE CYCLE|PARALEGAL"
    r"|ANALYST|COORDINATOR|ASSISTANT|REGISTRAR|ACCOUNTANT|BOOKKEEP)\b", re.I)
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "protonmail.com", "live.com", "msn.com", "comcast.net"}


def col_letter(idx):
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def norm_domain(w):
    w = (w or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    h = urlparse(w).netloc or ""
    return h[4:] if h.startswith("www.") else h


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


def pm(endpoint, body):
    try:
        r = requests.post(PM_BASE + endpoint,
                          headers={"Authorization": f"Bearer {PM_KEY}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=120)
    except requests.RequestException as e:
        return {}, f"error:{type(e).__name__}"
    if r.status_code == 429:
        return {}, "rate_limited"
    if r.status_code != 200:
        return {}, f"error:http_{r.status_code}"
    return (r.json() or {}), ""


def check(email, domain):
    """Valid + domain match + not a free mailbox."""
    if not email:
        return None, "no_email"
    d = email.split("@")[-1].lower()
    if d in FREE_MAIL:
        return None, "free_mailbox"
    if domain and d != domain and not (d.endswith("." + domain) or domain.endswith("." + d)):
        return None, f"domain_mismatch:{d}"
    return email, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=0,
                    help="stop once this many valid emails exist in the sheet")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent lookups (each row is up to 3 API calls)")
    ap.add_argument("--fresh", action="store_true",
                    help="run on rows nobody has attempted yet (PM as primary lane)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not PM_KEY and not args.dry_run:
        sys.exit("[pm] PURPLE_MAGIC_KEY missing in .claude/.env")

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:AZ").execute().get("values", [])

    def cell(row, i):
        return row[i].strip() if len(row) > i and row[i] else ""

    have = sum(1 for r in values if cell(r, C_EMAIL))
    # default: rows AMF already failed on (they carry a status).
    # --fresh: rows nobody has tried yet, to measure PM as the PRIMARY lane.
    todo = [(n, r, norm_domain(cell(r, C_WEBSITE)))
            for n, r in enumerate(values, start=2)
            if not cell(r, C_EMAIL) and cell(r, C_WEBSITE)
            and (not cell(r, C_STATUS) if args.fresh else cell(r, C_STATUS))]
    if args.limit:
        todo = todo[:args.limit]

    print(f"[pm] {have} valid emails already | {len(todo)} AMF-failed rows to retry"
          f"{' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run:
        for n, r, d in todo[:10]:
            print(f"    row{n:5d} {cell(r,C_COMPANY)[:34]:34s} {d[:26]:26s} "
                  f"| {cell(r,C_AO_NAME)[:22]} ({cell(r,C_STATUS)})")
        return

    def resolve(item):
        """One row -> (row_no, email, name, title, status). Thread-safe: only
        does HTTP, never touches shared state."""
        n, row, dom = item
        ao_name, ao_title = cell(row, C_AO_NAME), cell(row, C_AO_TITLE)
        parts = ao_name.split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        email = name = title = None
        status = ""

        if first and last and not BACK_OFFICE_RE.search(ao_title):
            d, err = pm("/find", {"firstName": first, "lastName": last, "domain": dom})
            if err == "rate_limited":
                return n, None, None, None, "rate_limited"
            if not err:
                if d.get("status") == "valid":
                    email, status = check(d.get("email"), dom)
                    status = status or ""
                    name, title = ao_name, ao_title
                else:
                    status = f"pm_{d.get('status') or 'not_found'}"
            else:
                status = f"pm_{err}"

        if not email:                       # try the org's own decision maker
            d, err = pm("/decision-makers", {"domain": dom})
            if err == "rate_limited":
                return n, None, None, None, "rate_limited"
            best = (d or {}).get("best") or {}
            if best.get("firstName") and best.get("lastName"):
                d2, err2 = pm("/find", {"firstName": best["firstName"],
                                        "lastName": best["lastName"], "domain": dom})
                if not err2 and d2.get("status") == "valid":
                    e2, s2 = check(d2.get("email"), dom)
                    if e2:
                        email = e2
                        name = best.get("fullName") or f"{best['firstName']} {best['lastName']}"
                        title = best.get("title") or "CEO"
                        status = ""
                    else:
                        status = s2
        return n, email, name, title, status

    # Concurrent driver. Each row costs up to three sequential API calls and
    # Purple Magic cascades providers internally, so serial execution ran ~10s
    # per row. Workers only do HTTP; all sheet writes happen here on the main
    # thread, one chunk at a time, so the batch-of-10 discipline still holds.
    updates, found, attempted = [], have, 0
    reasons = {}
    stop = False
    for i in range(0, len(todo), args.workers * 4):
        if stop or (args.target and found >= args.target):
            break
        chunk = todo[i:i + args.workers * 4]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(resolve, chunk))
        for n, email, name, title, status in results:
            if status == "rate_limited":
                print("\n[pm] 429 daily cap reached — stopping")
                stop = True
                continue
            attempted += 1
            if email:
                found += 1
                p = (name or "").split()
                for idx, val in ((C_DM_NAME, name), (C_DM_TITLE, title or "CEO"),
                                 (C_EMAIL, email), (C_FIRST, p[0] if p else ""),
                                 (C_LAST, p[-1] if len(p) > 1 else ""),
                                 (C_STATUS, "valid_pm")):
                    updates.append({"range": f"'{TAB}'!{col_letter(idx)}{n}",
                                    "values": [[val]]})
            else:
                key = status or "not_found"
                reasons[key] = reasons.get(key, 0) + 1
                updates.append({"range": f"'{TAB}'!{col_letter(C_STATUS)}{n}",
                                "values": [[f"pm_{key}"[:48]]]})
        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": updates}).execute()
            updates = []
        print(f"  attempted {attempted}/{len(todo)} | new {found-have} | "
              f"total valid {found}", end="\r")

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
    rescued = found - have
    print(f"\n[pm] {attempted} attempted -> {rescued} new emails "
          f"({100*rescued/max(attempted,1):.0f}%) | sheet total {found}")
    if reasons:
        print("  misses: " + ", ".join(f"{k} {v}" for k, v in
                                       sorted(reasons.items(), key=lambda x: -x[1])[:6]))


if __name__ == "__main__":
    main()
