"""Apollo Organization Enrichment for blank-size rows: writes real employee
count to col M and HQ state to col AM. ~1 Apollo credit per company (idle
pool). Resumable: skips rows whose M already contains a digit."""
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
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
SID = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
KEY = os.getenv("APOLLO_API_KEY")
WORKERS = 4
BATCH = 10

def norm_domain(w):
    w = (w or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    h = urlparse(w).netloc or ""
    return h[4:] if h.startswith("www.") else h

creds = Credentials.from_authorized_user_file(
    os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json"))
svc = build("sheets", "v4", credentials=creds)
vals = svc.spreadsheets().values().get(
    spreadsheetId=SID, range="Leads!A1:AM1025").execute().get("values", [])
header = vals[0]
# hq_state lives in AM — AC is reserved for the Indeed URL on every sheet (Jude's rule)
if len(header) <= 38 or not (header[38] or "").strip():
    svc.spreadsheets().values().update(
        spreadsheetId=SID, range="Leads!AM1", valueInputOption="RAW",
        body={"values": [["hq_state"]]}).execute()

todo = []
for i, r in enumerate(vals[1:], start=2):
    size = r[12].strip() if len(r) > 12 and r[12] else ""
    dom = norm_domain(r[11] if len(r) > 11 else "")
    status = r[27].strip() if len(r) > 27 and r[27] else ""
    if dom and not re.search(r"\d", size) and not status.startswith("skip"):
        todo.append((i, dom))
print(f"to enrich: {len(todo)}")

lock = threading.Lock()
results = {}
def enrich(item):
    row, dom = item
    for attempt in (1, 2):
        try:
            resp = requests.get(
                "https://api.apollo.io/api/v1/organizations/enrich",
                params={"domain": dom},
                headers={"x-api-key": KEY}, timeout=45)
        except requests.RequestException:
            return
        if resp.status_code == 429 and attempt == 1:
            time.sleep(60)
            continue
        if resp.status_code != 200:
            return
        org = (resp.json() or {}).get("organization") or {}
        n = org.get("estimated_num_employees")
        st = org.get("state") or ""
        with lock:
            results[row] = (str(n) if n else "", st)
        return

done = 0
for start in range(0, len(todo), BATCH):
    chunk = todo[start:start + BATCH]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(enrich, chunk))
    data = []
    for row, _ in chunk:
        if row in results:
            n, st = results[row]
            if n:
                data.append({"range": f"Leads!M{row}", "values": [[n]]})
            if st:
                data.append({"range": f"Leads!AM{row}", "values": [[st]]})
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SID,
            body={"valueInputOption": "RAW", "data": data}).execute()
    done += len(chunk)
    print(f"  {done}/{len(todo)} (sized: {len(results)})")

print(f"Done. Enriched {len(results)}/{len(todo)}")
