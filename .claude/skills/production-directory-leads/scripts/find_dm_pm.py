"""
Find decision-maker + verified email for production houses via Purple Magic
(ConnectorOS) — PM-PRIMARY lane (Jude, 2026-08-11: run PM before Apollo/AMF
for this vertical, not after). Adapted from
healthcare-staffing-enrichment/find_ceo_pm_demand.py, same mechanics:
/decision-makers {domain} -> best+others -> positive owner-like title gate
BEFORE the /find call -> /find {firstName,lastName,domain} -> valid-only
email. The gate runs first so non-authority people never spend a lookup.

DM target (Jude, 2026-08-11, no campaign data yet for this vertical — this is
a starting hypothesis): Owner / Founder / CEO / President / Managing
Director / Managing Partner / Principal. Nobody else. Same OWNER_RE as the
healthcare-staffing PM script — it already matched what Jude described
verbatim, reused rather than reinvented.

Schema: the repo's 29-col base layout (K:Company Name, L:Website, T:DM Name,
U:DM Title, V:LinkedIn URL, W:Email, X:First Name, Y:Last Name). Writes its
OWN attempt status to AC ("PM Status"), never to AB — AB is
apollo_dm_waterfall_production.py's status column, and its skip logic keys
off col_email(W) or a non-empty AB. Leaving AB untouched means a PM miss
(blank W, blank AB) is picked up automatically by the Apollo/AMF fallback
pass with zero extra wiring; a PM find (filled W) is correctly skipped by it.

Validations (same standing rules as every email pass in this repo):
  1. Only PM status == "valid" emails are written.
  2. Email domain must match the company domain (cross-company rejected).
  3. Free mailboxes rejected.
  4. Name/title written only when a valid email was found (no partial data).
  5. Positive title gate: owner/CEO-like titles pass, everything else —
     including unknown/blank titles — fails (empty beats marginal).

Resume-safe: rows with any AC status are skipped. Batch-of-10 writes.

Run:
  python3 -W ignore find_dm_pm.py --sheet_url "URL" [--tab Leads] \
      [--limit 50] [--workers 8] [--dry_run]
"""

import os
import re
import json
import time
import threading
import argparse
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

PM_KEY = os.getenv("PURPLE_MAGIC_KEY")
PM_BASE = "https://api.connector-os.com/api/email/v2"

HOSTING_DOMAINS = {
    "squarespace.com", "wix.com", "wixsite.com", "weebly.com", "wordpress.com",
    "webflow.io", "webflow.com", "godaddy.com", "shopify.com", "myshopify.com",
    "netlify.app", "vercel.app", "github.io", "carrd.co", "strikingly.com",
    "lovable.app", "framer.app", "framer.site", "bubble.io", "glide.page",
    "linktr.ee", "linktree.com", "bio.link", "beacons.ai",
    "mailchimp.com", "hubspot.com", "typeform.com",
}
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "protonmail.com", "live.com", "msn.com", "comcast.net"}

# Positive gate: budget authority at a production house. Unknown fails.
OWNER_RE = re.compile(
    r"\b(owner|founder|co[- ]?founder|ceo|chief executive|president"
    r"|managing director|managing partner|principal)\b", re.I)
NOT_OWNER_RE = re.compile(
    r"\b(assistant|associate|advisor to|office of|intern|former|ex[- ])\b", re.I)

COL_NAME = 10        # K
COL_WEBSITE = 11     # L
COL_DM_NAME = 19     # T
COL_DM_TITLE = 20    # U
COL_DM_LINKEDIN = 21 # V
COL_DM_EMAIL = 22    # W
COL_FIRST = 23       # X
COL_LAST = 24        # Y
COL_PM_STATUS = 28   # AC — new column, never read by the Apollo fallback

WRITE_BATCH = 10


def col_letter(idx):
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_sheet_id(url):
    return url.split("/d/")[1].split("/")[0]


def get_service():
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


def norm_domain(w):
    w = (w or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    h = urlparse(w).netloc or ""
    h = h[4:] if h.startswith("www.") else h
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", h):
        return ""
    root = ".".join(h.split(".")[-2:])
    return "" if root in HOSTING_DOMAINS else h


CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "data", "pm_dm_cache.jsonl")
_cache = {}
_cache_lock = threading.Lock()


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    _cache[rec["domain"]] = rec["response"]
                except (json.JSONDecodeError, KeyError):
                    continue
    print(f"PM cache: {len(_cache)} domains loaded", flush=True)


def cache_put(domain, response):
    with _cache_lock:
        _cache[domain] = response
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "a") as f:
            f.write(json.dumps({"domain": domain, "response": response,
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")


def pm_decision_makers(domain):
    with _cache_lock:
        if domain in _cache:
            return _cache[domain], ""
    d, err = pm("/decision-makers", {"domain": domain})
    if not err:
        cache_put(domain, d)
    return d, err


def pm(endpoint, body):
    for attempt in range(3):
        try:
            r = requests.post(PM_BASE + endpoint,
                              headers={"Authorization": f"Bearer {PM_KEY}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=120)
        except requests.RequestException as e:
            if attempt == 2:
                return {}, f"error:{type(e).__name__}"
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code != 200:
            return {}, f"error:http_{r.status_code}"
        return (r.json() or {}), ""
    return {}, "rate_limited"


def check_email(email, domain):
    if not email:
        return None
    d = email.split("@")[-1].lower()
    if d in FREE_MAIL:
        return None
    if domain and d != domain and not (d.endswith("." + domain) or domain.endswith("." + d)):
        return None
    return email


TITLE_PRIORITY = [
    r"\bceo\b|chief executive", r"\bowner\b", r"\bfounder\b",
    r"\bpresident\b", r"managing partner", r"managing director",
    r"\bprincipal\b",
]


def title_rank(title):
    for i, pat in enumerate(TITLE_PRIORITY):
        if re.search(pat, title, re.I):
            return i
    return len(TITLE_PRIORITY)


def resolve(target):
    dom = target["domain"]
    d, err = pm_decision_makers(dom)
    if err:
        return {**target, "status": f"pm_{err}"}
    cands, seen = [], set()
    for c in [(d or {}).get("best") or {}] + ((d or {}).get("others") or []):
        fn = c.get("fullName")
        if fn and fn not in seen:
            seen.add(fn)
            cands.append(c)
    if not cands:
        return {**target, "status": "pm_no_dm"}
    gated = [c for c in cands
             if OWNER_RE.search((c.get("title") or "").strip())
             and not NOT_OWNER_RE.search(c.get("title") or "")]
    if not gated:
        return {**target, "status": "pm_bad_title"}
    gated.sort(key=lambda c: (title_rank(c.get("title") or ""),
                              -(c.get("seniorityScore") or 0)))
    for c in gated:
        first, last = c.get("firstName"), c.get("lastName")
        if not (first and last):
            parts = (c.get("fullName") or "").split()
            first, last = (parts[0], parts[-1]) if len(parts) >= 2 else (None, None)
        if not (first and last):
            continue
        d2, err2 = pm("/find", {"firstName": first, "lastName": last, "domain": dom})
        if err2:
            return {**target, "status": f"pm_{err2}"}
        if d2.get("status") != "valid":
            continue
        email = check_email(d2.get("email"), dom)
        if not email:
            continue
        return {**target, "status": "found",
                "dm_name": c.get("fullName") or f"{first} {last}",
                "dm_title": (c.get("title") or "").strip(),
                "dm_email": email, "first": first, "last": last,
                "dm_linkedin": c.get("linkedIn") or ""}
    return {**target, "status": "pm_not_found"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retry_pm", action="store_true",
                    help="also redo rows already stamped pm_* (cached domains replay free)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    if not PM_KEY and not args.dry_run:
        print("ERROR: PURPLE_MAGIC_KEY not set")
        return

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()

    if not args.dry_run:
        # Sheets grids from export_batch.py/consolidate_master.py are sized
        # to exactly 28 columns (A-AB) — a values.get/update on AC 400s with
        # "exceeds grid limits" rather than auto-expanding. Grow the grid
        # once before touching AC.
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_props = next(s["properties"] for s in meta["sheets"]
                           if s["properties"]["title"] == tab)
        if sheet_props["gridProperties"].get("columnCount", 0) < 29:
            service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": sheet_props["sheetId"],
                                  "gridProperties": {"columnCount": 29}},
                    "fields": "gridProperties.columnCount"}}]}).execute()

        hdr = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!AC1").execute().get("values", [])
        if not hdr or not (hdr[0] and hdr[0][0].strip()):
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"'{tab}'!AC1",
                valueInputOption="RAW", body={"values": [["PM Status"]]}).execute()

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:AC"
    ).execute().get("values", [])[1:]

    def cell(row, i):
        return (row[i] if len(row) > i else "").strip()

    targets = []
    for i, row in enumerate(rows):
        name = cell(row, COL_NAME)
        email = cell(row, COL_DM_EMAIL)
        status = cell(row, COL_PM_STATUS)
        pending = not email and ((not status) or (args.retry_pm and status.startswith("pm_")))
        domain = norm_domain(cell(row, COL_WEBSITE))
        if name and pending and domain:
            targets.append({"row": i + 2, "name": name, "domain": domain})
    if args.limit:
        targets = targets[:args.limit]

    print(f"=== Find DM (Purple Magic, PM-primary) — tab '{tab}' ===", flush=True)
    load_cache()
    print(f"Candidates to query: {len(targets)}", flush=True)
    if args.dry_run:
        for t in targets[:10]:
            print(f"  row{t['row']:5d} {t['name'][:40]:40s} {t['domain']}")
        return

    lock = threading.Lock()
    updates, found, missed = [], 0, 0

    def flush():
        data = []
        for u in updates:
            r = u["row"]
            if u["status"] == "found":
                data.append({"range": f"'{tab}'!{col_letter(COL_DM_NAME)}{r}",
                             "values": [[u["dm_name"]]]})
                data.append({"range": f"'{tab}'!{col_letter(COL_DM_TITLE)}{r}",
                             "values": [[u["dm_title"]]]})
                data.append({"range": f"'{tab}'!{col_letter(COL_DM_EMAIL)}{r}",
                             "values": [[u["dm_email"]]]})
                data.append({"range": f"'{tab}'!{col_letter(COL_FIRST)}{r}",
                             "values": [[u["first"]]]})
                data.append({"range": f"'{tab}'!{col_letter(COL_LAST)}{r}",
                             "values": [[u["last"]]]})
                if u.get("dm_linkedin"):
                    data.append({"range": f"'{tab}'!{col_letter(COL_DM_LINKEDIN)}{r}",
                                 "values": [[u["dm_linkedin"]]]})
            data.append({"range": f"'{tab}'!{col_letter(COL_PM_STATUS)}{r}",
                         "values": [[u["status"]]]})
        for attempt in range(4):
            try:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": data}).execute()
                break
            except Exception as e:
                if attempt < 3 and "429" in str(e):
                    time.sleep(65)
                else:
                    raise
        updates.clear()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(resolve, t) for t in targets]
        done = 0
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                updates.append(res)
                done += 1
                if res["status"] == "found":
                    found += 1
                    print(f"  +  {res['name'][:42]:42s} -> {res['dm_name']} "
                          f"({res['dm_title']}) | {res['dm_email']}", flush=True)
                else:
                    missed += 1
                if len(updates) >= WRITE_BATCH:
                    flush()
                if done % 100 == 0:
                    print(f"  Progress: {done}/{len(targets)} "
                          f"(found {found}, missed {missed})", flush=True)
        if updates:
            flush()

    print(f"\nDone. found={found}  missed={missed}  of {len(targets)}", flush=True)
    print("Misses have a blank W (email) and blank AB (Apollo status) — "
          "run apollo_dm_waterfall_production.py next to pick them up.")


if __name__ == "__main__":
    main()
