"""Fill emails for rows that ALREADY have a decision maker's name.

Use this, not /decision-maker, whenever the DM's identity is already known:

    /find-email/person        1 credit per found   <- we know WHO we want
    /find-email/decision-maker 2 credits per found  <- resolves a ROLE at a domain

Paying 2 credits to re-resolve a role we already identified is money burned. The
waterfall's identity-only mode (--skip_email) deliberately leaves these rows
with name + title + LinkedIn and no email; this is the pass that completes them.

Non-negotiables, same as everywhere else in the pipeline:
  * VALID EMAILS ONLY — `risky` is rejected, no exceptions.
  * The email's domain MUST match the company's, or it is discarded. AMF's
    person endpoint can return a different same-first-name person's mailbox
    (~29% of initial-only finds in the first campaign), and this is the guard
    that catches it before a send.
  * Rows that already carry an email are never touched.

  python3 -W ignore amf_person_fill.py --sheet_url "URL" --limit 50        # dry run
  python3 -W ignore amf_person_fill.py --sheet_url "URL" --limit 50 --apply
"""
import os
import re
import argparse
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"


def norm_domain(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0]


def root(d):
    p = (d or "").split(".")
    return p[-2] if len(p) >= 2 else d


def amf_person(full_name, domain):
    """(email, credits, reason). VALID + domain-matched only."""
    try:
        r = requests.post(AMF_PERSON_URL,
                          headers={"Authorization": AMF_KEY,
                                   "Content-Type": "application/json"},
                          json={"full_name": full_name, "domain": domain},
                          timeout=120)
    except requests.RequestException as e:
        return None, 0, f"error:{type(e).__name__}"
    if r.status_code in (402, 429):
        return None, 0, f"error:http_{r.status_code}"
    if r.status_code != 200:
        return None, 0, f"error:http_{r.status_code}"
    d = r.json() or {}
    # Distinguish "AMF has nothing" from "AMF found one but it is risky".
    # Lumping them together as not_valid implied a pool of risky emails being
    # rejected by policy, when the real answer was that AMF had no record.
    es = d.get("email_status")
    if es != "valid":
        return None, 0, ("amf_has_no_record" if es == "not_found"
                         else f"rejected_{es or 'unknown'}")
    email = (d.get("email") or d.get("valid_email") or "").strip()
    if not email or "@" not in email:
        return None, 0, "no_email"
    if root(email.split("@")[1]) != root(domain):
        return None, 0, f"domain_mismatch:{email.split('@')[1]}"
    return email, 1, None


ap = argparse.ArgumentParser()
ap.add_argument("--sheet_url", required=True)
ap.add_argument("--tab", default="Leads")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

if not AMF_KEY:
    raise SystemExit("ANYMAILFINDER_API_KEY not set")

SID = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
svc = build("sheets", "v4",
            credentials=Credentials.from_authorized_user_file(TOKEN_PATH))
vals = svc.spreadsheets().values().get(
    spreadsheetId=SID, range=f"{args.tab}!A1:AH3000").execute().get("values", [])
hdr, rows = vals[0], vals[1:]
ix = {x.strip().lower(): i for i, x in enumerate(hdr)}
NM, WEB, DM, EM, ST = (ix["company name"], ix["company website"],
                       ix["dm name"], ix["email"], 29)


def g(r, i):
    return (r[i] if len(r) > i else "").strip()


todo = []
for i, r in enumerate(rows):
    if g(r, EM) or not g(r, DM):
        continue
    dom = norm_domain(g(r, WEB))
    if not dom or "." not in dom:
        continue
    todo.append({"row": i + 2, "name": g(r, DM), "domain": dom,
                 "company": g(r, NM), "src": g(r, ST)})
if args.limit:
    todo = todo[:args.limit]

src = Counter(t["src"].split("_p")[0] for t in todo)
print(f"=== AMF person fill {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
print(f"rows with a DM name but no email : {len(todo)}")
print(f"  by source: {dict(src)}")
print(f"worst-case credits               : {len(todo)} (1 per FOUND; misses free)")
if not args.apply:
    print("\n[DRY RUN] no AMF calls, nothing spent. Re-run with --apply.")
    for t in todo[:10]:
        print(f"   {t['company'][:32]:34}{t['name'][:24]:26}{t['domain']}")
    raise SystemExit

lock = threading.Lock()
stats = Counter()
writes = []


def work(t):
    email, cr, why = amf_person(t["name"], t["domain"])
    with lock:
        stats["credits"] += cr
        if email:
            stats["found"] += 1
            writes.append((t["row"], email, f"{t['src']}+amf_person"))
        else:
            stats[why or "miss"] += 1
            writes.append((t["row"], None, None))


with ThreadPoolExecutor(max_workers=args.workers) as pool:
    list(pool.map(work, todo))

data = []
for row, email, tag in writes:
    if email:
        data += [{"range": f"{args.tab}!W{row}", "values": [[email]]},
                 {"range": f"{args.tab}!AD{row}", "values": [[tag]]}]
for k in range(0, len(data), 100):
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SID,
        body={"valueInputOption": "RAW", "data": data[k:k + 100]}).execute()

print("\n=== Summary ===")
print(f"  emails written : {stats['found']}/{len(todo)} "
      f"({100*stats['found']/max(1,len(todo)):.0f}%)")
print(f"  credits spent  : {stats['credits']}")
for k, n in stats.most_common():
    if k not in ("found", "credits"):
        print(f"    {k:28} {n}")
