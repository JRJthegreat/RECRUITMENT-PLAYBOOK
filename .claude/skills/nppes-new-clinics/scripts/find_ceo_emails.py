"""
Phase 4 — resolve every row to a CEO-level verified email, on the campaign sheet.

Jude's rule (2026-07-23): ALWAYS aim at the CEO. NPPES names an Authorized
Official on every filing, but that person is only the CEO ~65% of the time, so:

  AO title is owner-like  -> AMF /find-email/person   (1 credit, we know the name)
  AO title is anything else -> AMF /find-email/decision-maker category=ceo
                               (2 credits, AMF finds the CEO for us)
  person endpoint misses  -> fall back to /decision-maker ceo anyway

Only `email_status == "valid"` is written, and the email's domain must match the
resolved website — a mismatch means AMF found a real person at the wrong company.
Never writes a name/title without a valid email (no partial rows).

Stops as soon as --target valid emails exist in the sheet, so you pay for what
you need and nothing more.

Sheet columns (campaign layout from build_campaign_sheet.py):
  L website | T DM name | U DM title | W email | X first | Y last
  AC filed-by name | AD filed-by title | AF email_status

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/find_ceo_emails.py \
      --sheet_url URL --target 400 --dry_run
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
DM_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
TAB = "Leads"

C_WEBSITE, C_DM_NAME, C_DM_TITLE = 11, 19, 20      # L, T, U
C_EMAIL, C_FIRST, C_LAST = 22, 23, 24              # W, X, Y
C_COMPANY, C_AO_NAME, C_AO_TITLE, C_STATUS = 10, 28, 29, 31   # K, AC, AD, AF

# ROUTING (Jude, 2026-07-23): "NPI already gives you who filed. In a lot of
# cases this is the right person to contact. Do not do decision maker for all."
#
# So the NPPES filer is the DEFAULT target, whatever their title — at a small
# practice the Owner, the Executive Director, the Administrator and even the
# Physical Therapist or Nurse Practitioner who filed ARE the founder. Only a
# thin back-office slice is genuinely the wrong human, and only those go to
# /decision-maker. This is both more accurate and half the credit cost.
BACK_OFFICE_RE = re.compile(
    r"\b(CFO|CHIEF FINANCIAL|VP FINANCE|VICE PRESIDENT.{0,12}FINANCE|FINANCE"
    r"|CONTROLLER|TREASURER|SECRETARY|CREDENTIALING|COMPLIANCE|LEGAL|COUNSEL"
    r"|BILLING|REVENUE CYCLE|PARALEGAL|ANALYST|COORDINATOR|ASSISTANT"
    r"|REGISTRAR|ACCOUNTANT|BOOKKEEP)\b", re.I)

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "protonmail.com", "live.com", "msn.com", "comcast.net"}


def col_letter(idx):
    s = ""
    idx += 1
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


def amf(url, payload):
    try:
        r = requests.post(url, headers={"Authorization": AMF_KEY,
                                        "Content-Type": "application/json"},
                          json=payload, timeout=180)
    except requests.RequestException as e:
        return {}, 0, f"error:{type(e).__name__}"
    if r.status_code == 402:
        return {}, 0, "no_credits"
    if r.status_code != 200:
        return {}, 0, f"error:http_{r.status_code}"
    d = r.json() or {}
    credits = d.get("credits_charged", 0) or 0
    return d, credits, ""


def accept(d, domain):
    """Valid + domain must match the resolved website. AMF returning a person
    on a different domain means it matched the wrong company."""
    if d.get("email_status") != "valid":
        return None, None, None, f"rejected_{d.get('email_status') or 'unknown'}"
    email = d.get("valid_email") or d.get("email")
    if not email:
        return None, None, None, "no_email"
    edom = email.split("@")[-1].lower()
    if edom in FREE_MAIL:
        return None, None, None, "free_mailbox"
    if domain and edom != domain and not (edom.endswith("." + domain) or domain.endswith("." + edom)):
        return None, None, None, f"domain_mismatch:{edom}"
    return email, (d.get("person_full_name") or "").strip(), (d.get("person_job_title") or "").strip(), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--target", type=int, default=400,
                    help="stop once this many valid emails exist in the sheet")
    ap.add_argument("--limit", type=int, default=0, help="max rows to attempt")
    ap.add_argument("--dm_rescue", action="store_true",
                    help="also try /decision-maker when the filer lookup misses "
                         "(2 extra credits per rescued row; OFF by default)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not AMF_KEY and not args.dry_run:
        sys.exit("[ceo] ANYMAILFINDER_API_KEY missing in .claude/.env")

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:AZ").execute().get("values", [])

    def cell(row, i):
        return row[i].strip() if len(row) > i and row[i] else ""

    have = sum(1 for r in values if cell(r, C_EMAIL))
    todo = []
    for n, row in enumerate(values, start=2):
        if cell(row, C_EMAIL) or cell(row, C_STATUS):
            continue                       # already resolved or already tried
        dom = norm_domain(cell(row, C_WEBSITE))
        if not dom:
            continue                       # no website -> nothing to search
        todo.append((n, row, dom))
    if args.limit:
        todo = todo[:args.limit]

    print(f"[ceo] {have} valid emails already | target {args.target} | "
          f"{len(todo)} rows with a domain and no result yet"
          f"{' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run:
        n_dm = sum(1 for _, r, _ in todo
                   if BACK_OFFICE_RE.search(cell(r, C_AO_TITLE)) or not cell(r, C_AO_NAME))
        print(f"  routing: {len(todo)-n_dm} -> /person on the NPPES filer (1 credit)")
        print(f"           {n_dm} -> /decision-maker ceo (back-office filer, 2 credits)")
        for n, r, d in todo[:12]:
            route = ("dm:ceo" if BACK_OFFICE_RE.search(cell(r, C_AO_TITLE))
                     or not cell(r, C_AO_NAME) else "person")
            print(f"    row{n:5d} {route:7s} {cell(r,C_COMPANY)[:34]:34s} {d[:28]:28s} "
                  f"| filed by {cell(r,C_AO_NAME)[:20]} ({cell(r,C_AO_TITLE)[:18]})")
        return

    updates, credits, found = [], 0, have
    attempted = 0
    for n, row, dom in todo:
        if found >= args.target:
            print(f"\n[ceo] target {args.target} reached — stopping")
            break
        ao_name, ao_title = cell(row, C_AO_NAME), cell(row, C_AO_TITLE)
        email = name = title = None
        status = ""
        back_office = bool(BACK_OFFICE_RE.search(ao_title)) or not ao_name

        if not back_office:                 # DEFAULT: the filer is the target
            d, c, err = amf(PERSON_URL, {"full_name": ao_name, "domain": dom})
            credits += c
            if err == "no_credits":
                print("\n[ceo] AMF out of credits — stopping"); break
            email, name, title, status = accept(d, dom) if not err else (None, None, None, err)
            if email:
                name, title = name or ao_name, title or ao_title

        # /decision-maker ONLY for back-office filers, or on an explicit rescue
        if not email and (back_office or args.dm_rescue):
            d, c, err = amf(DM_URL, {"domain": dom, "decision_maker_category": "ceo"})
            credits += c
            if err == "no_credits":
                print("\n[ceo] AMF out of credits — stopping"); break
            e2, n2, t2, s2 = accept(d, dom) if not err else (None, None, None, err)
            if e2:
                email, name, title, status = e2, n2, t2, ""
            else:
                status = status or s2

        attempted += 1
        if email:
            found += 1
            parts = (name or "").split()
            first = parts[0] if parts else ""
            last = parts[-1] if len(parts) > 1 else ""
            for idx, val in ((C_DM_NAME, name), (C_DM_TITLE, title or "CEO"),
                             (C_EMAIL, email), (C_FIRST, first), (C_LAST, last),
                             (C_STATUS, "valid")):
                updates.append({"range": f"'{TAB}'!{col_letter(idx)}{n}", "values": [[val]]})
        else:
            updates.append({"range": f"'{TAB}'!{col_letter(C_STATUS)}{n}",
                            "values": [[status or "not_found"]]})

        if len(updates) >= 30:              # batch-of-10 rows (up to 6 cells each)
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": updates}).execute()
            updates = []
        print(f"  attempted {attempted}/{len(todo)} | valid {found}/{args.target} "
              f"| credits {credits}", end="\r")
        time.sleep(0.15)

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
    print(f"\n[ceo] {attempted} attempted -> {found} valid CEO emails in sheet "
          f"| {credits} AMF credits charged")


if __name__ == "__main__":
    main()
