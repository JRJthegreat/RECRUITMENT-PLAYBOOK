"""AMF /decision-maker (category=ceo) rescue for confirmed-TINY Indiana rows
where Apollo had no people. 2 credits per found valid email; misses free.
Approved by Jude: small orgs only, CEO category only."""
import os
import re
import requests
from dotenv import load_dotenv
import argparse
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from urllib.parse import urlparse

ap = argparse.ArgumentParser()
ap.add_argument("--sheet_url", required=True)
args = ap.parse_args()
import re as _re
SID = _re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
URL = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

def norm_domain(w):
    w = (w or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    h = urlparse(w).netloc or ""
    return h[4:] if h.startswith("www.") else h

def band(size):
    m = re.search(r"([\d,]+)", size or "")
    if not m: return "TINY"
    lb = int(m.group(1).replace(",", ""))
    return "LARGE" if lb >= 500 else ("MID" if lb >= 50 else "TINY")

creds = Credentials.from_authorized_user_file(
    os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json"))
svc = build("sheets", "v4", credentials=creds)
vals = svc.spreadsheets().values().get(
    spreadsheetId=SID, range="Leads!A1:AB1025").execute().get("values", [])

def cell(r, i):
    return r[i].strip() if len(r) > i and r[i] else ""

todo = []
for i, r in enumerate(vals[1:], start=2):
    if cell(r, 27) != "no_apollo_people":
        continue
    if band(cell(r, 12)) != "TINY":
        continue
    dom = norm_domain(cell(r, 11))
    if dom:
        todo.append((i, dom, cell(r, 10)))
print(f"rescue candidates: {len(todo)}")

found = credits = 0
pending = []
def flush():
    global pending
    if pending:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SID,
            body={"valueInputOption": "RAW", "data": pending}).execute()
        pending = []

for n, (row, dom, company) in enumerate(todo, 1):
    try:
        resp = requests.post(
            URL, headers={"Authorization": AMF_KEY,
                          "Content-Type": "application/json"},
            json={"domain": dom, "decision_maker_category": "ceo"},
            timeout=180)
        d = resp.json()
    except Exception as e:
        print(f"  [!] {company}: {type(e).__name__}")
        continue
    credits += d.get("credits_charged", 0) or 0
    if d.get("email_status") == "valid" and d.get("valid_email"):
        email = d["valid_email"]
        full = (d.get("person_full_name") or "").strip()
        title = (d.get("person_job_title") or "CEO").strip()
        parts = full.split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        pending.append({"range": f"Leads!T{row}:Y{row}", "values": [[
            full or "CEO", title, "", email, first, last]]})
        pending.append({"range": f"Leads!AB{row}",
                        "values": [["found_amf_dm_ceo"]]})
        found += 1
        print(f"  ✓ {company}: {full or 'CEO'} <{email}>")
    else:
        pending.append({"range": f"Leads!AB{row}",
                        "values": [["rescue_not_found"]]})
    if n % 10 == 0:
        flush()
        print(f"  -- {n}/{len(todo)} | found {found} | credits {credits}")
flush()
print(f"Done. Found {found}/{len(todo)}, AMF credits: {credits}")
