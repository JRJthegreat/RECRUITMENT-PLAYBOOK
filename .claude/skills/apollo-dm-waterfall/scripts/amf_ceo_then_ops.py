"""AMF /decision-maker pass, sequenced per Jude (2026-08-01).

Apollo only knows who it has indexed. AnyMail Finder's /decision-maker resolves
a role at a DOMAIN, so it reaches companies Apollo has nobody for — which is the
268-row blind spot the waterfall could not touch.

Order, per row:
  1. AMF /decision-maker category=ceo
       - row already has an Apollo ADMIN  -> a CEO hit UPGRADES it; a miss keeps
         the admin exactly as it was (never downgrade what we already hold)
       - row has nothing                  -> a CEO hit fills it
  2. AMF /decision-maker category=operations  — only when step 1 missed AND the
     row still has no contact at all. "operations" is AMF's nearest valid
     category to a practice administrator; "coo" and "admin" are NOT valid
     categories and 400.

Rules kept from the rest of the pipeline:
  * VALID EMAILS ONLY — `risky` is rejected everywhere, no exceptions.
  * The email's domain must match the company's, or it is discarded (AMF
    occasionally returns a parent/reseller mailbox).
  * Rows carrying an owner-ladder DM (dm_only_*) are LEFT ALONE — they are the
    evidence-backed rung and there is nothing to upgrade to.
  * Batch-of-10 writes, resume-safe.

Costs 2 AMF credits per FOUND valid email; misses are free.

  python3 -W ignore amf_ceo_then_ops.py --sheet_url "URL" --limit 50   # dry run
  python3 -W ignore amf_ceo_then_ops.py --sheet_url "URL" --limit 50 --apply
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
AMF_URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

BATCH = 10


def norm_domain(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0]


def root(d):
    p = (d or "").split(".")
    return p[-2] if len(p) >= 2 else d


def amf(domain, company, category):
    """Return (email, full_name, title, credits, err). VALID only."""
    try:
        r = requests.post(AMF_URL,
                          headers={"Authorization": AMF_KEY,
                                   "Content-Type": "application/json"},
                          json={"domain": domain, "company_name": company,
                                "decision_maker_category": category},
                          timeout=60)
    except requests.RequestException as e:
        return None, "", "", 0, f"error:{type(e).__name__}"
    if r.status_code in (402, 429):
        return None, "", "", 0, f"error:http_{r.status_code}"
    if r.status_code == 400:
        return None, "", "", 0, "error:bad_request"
    if r.status_code != 200:
        return None, "", "", 0, f"error:http_{r.status_code}"
    d = r.json() or {}
    es = d.get("email_status")
    if es != "valid":
        return None, "", "", 0, ("amf_has_no_record" if es == "not_found"
                                 else f"rejected_{es or 'unknown'}")
    email = (d.get("email") or d.get("valid_email") or "").strip()
    if not email or "@" not in email:
        return None, "", "", 0, "no_email"
    # domain guard — a parent/reseller mailbox is the wrong company
    if root(email.split("@")[1]) != root(domain):
        return None, "", "", 0, f"domain_mismatch:{email.split('@')[1]}"
    name = " ".join(x for x in [d.get("person_first_name") or "",
                                d.get("person_last_name") or ""] if x).strip()
    return email, name, (d.get("person_job_title") or "").strip(), 2, None


def col(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


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
NM, WEB, DM, DT, EM = (ix["company name"], ix["company website"],
                       ix["dm name"], ix["dm title"], ix["email"])
FN, LN, ST = ix["first name"], ix["last name"], 29


def g(r, i):
    return (r[i] if len(r) > i else "").strip()


todo = []
for i, r in enumerate(rows):
    st = g(r, ST)
    if g(r, EM):                       # already has an email
        continue
    if st.startswith("dm_only"):       # owner-ladder row — nothing to upgrade to
        continue
    dom = norm_domain(g(r, WEB))
    if not dom or "." not in dom:
        continue
    todo.append({"row": i + 2, "company": g(r, NM), "domain": dom,
                 "has_admin": bool(g(r, DM)), "admin_name": g(r, DM),
                 "admin_title": g(r, DT)})
if args.limit:
    todo = todo[:args.limit]

up = sum(1 for t in todo if t["has_admin"])
print(f"=== AMF decision-maker: ceo -> operations {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
print(f"rows queued          : {len(todo)}")
print(f"  upgrade attempts   : {up}  (already hold an Apollo admin)")
print(f"  empty rows         : {len(todo)-up}")
print(f"worst-case credits   : {len(todo)*2} (2 per FOUND; misses are free)")
if not args.apply:
    print("\n[DRY RUN] no AMF calls, nothing spent. Re-run with --apply.")
    raise SystemExit

lock = threading.Lock()
stats = Counter()
writes = []


def work(t):
    email, name, title, cr, err = amf(t["domain"], t["company"], "ceo")
    used = "ceo"
    if not email and not t["has_admin"]:
        email, name, title, cr2, err2 = amf(t["domain"], t["company"], "operations")
        cr += cr2
        used = "operations"
    with lock:
        stats["credits"] += cr
    if not email:
        with lock:
            stats["no_find" if not t["has_admin"] else "kept_admin"] += 1
            writes.append((t["row"], None, None, None,
                           "amf_none" if not t["has_admin"] else "amf_kept_admin"))
        return
    parts = name.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    tag = (f"amf_{used}_upgrade" if t["has_admin"] else f"amf_{used}_new")
    with lock:
        stats[tag] += 1
        writes.append((t["row"], email, name or t["admin_name"],
                       title or "CEO", tag, first, last))


with ThreadPoolExecutor(max_workers=args.workers) as pool:
    list(pool.map(work, todo))

data = []
for w in writes:
    row, email = w[0], w[1]
    if email:
        _, _, name, title, tag, first, last = w
        data += [
            {"range": f"{args.tab}!T{row}", "values": [[name]]},
            {"range": f"{args.tab}!U{row}", "values": [[title]]},
            {"range": f"{args.tab}!W{row}", "values": [[email]]},
            {"range": f"{args.tab}!X{row}", "values": [[first]]},
            {"range": f"{args.tab}!Y{row}", "values": [[last]]},
            {"range": f"{args.tab}!AD{row}", "values": [[tag]]},
        ]
    else:
        data.append({"range": f"{args.tab}!AD{row}", "values": [[w[4]]]})
for k in range(0, len(data), 100):
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SID,
        body={"valueInputOption": "RAW", "data": data[k:k + 100]}).execute()

print("\n=== Summary ===")
for k, n in stats.most_common():
    print(f"  {k:24} {n}")
