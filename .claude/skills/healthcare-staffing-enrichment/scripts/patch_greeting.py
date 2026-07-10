"""
One-off: change the email greeting from "Hey" to "Hi" everywhere for the
healthcare recruitment-firm DEMAND campaign, AFTER it was already generated/pushed.

Propagates the change to all three places it lives:
  1. Sheet col V (email_body)        — leading "<div>Hey " -> "<div>Hi "
  2. Instantly campaign sequence      — FU1 + FU2 "Hey {{firstName}}" -> "Hi {{firstName}}"
     (step 1 stays "<div>{{personalization}}</div>" — greeting lives in the lead var)
  3. Each pushed lead's personalization — leading "<div>Hey " -> "<div>Hi "

Idempotent: rows/leads already saying "Hi" are left alone.

Run:
  python3 -W ignore patch_greeting.py --sheet_url "URL" --tab "TAB" \
    --campaign_id ID [--dry_run]
"""

import os
import re
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"

COL_DM_EMAIL     = 16  # Q
COL_EMAIL_STATUS = 18  # S
COL_BODY         = 21  # V

OLD_GREETING = "<div>Hey "
NEW_GREETING = "<div>Hi "

SUBJECT = "new healthcare reqs"

FU1 = ("<div>Hi {{firstName}},</div><div><br /></div>"
       "<div>Floating this back up. The healthcare employers I'm talking to are moving on their "
       "expansion hiring now, so the timing's better than waiting a few months.</div><div><br /></div>"
       "<div>Let me know if you're open to some warm intros.</div><div><br /></div>"
       "<div>Best,</div><div>Jude</div>")

FU2 = ("<div>Hi {{firstName}},</div><div><br /></div>"
       "<div>Last note from me. If connecting with healthcare employers before the roles go live "
       "ever becomes a priority, feel free to re-open. I'm one reply away.</div><div><br /></div>"
       "<div>Best,</div><div>Jude</div>")

WRITE_BATCH = 10


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def headers():
    return {"Authorization": f"Bearer {INSTANTLY_API_KEY}", "Content-Type": "application/json"}


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def list_leads(campaign_id):
    leads, starting_after = [], None
    while True:
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        resp = requests.post(f"{INSTANTLY_BASE}/leads/list", headers=headers(), json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        for it in items:
            leads.append({"id": it["id"], "email": (it.get("email") or "").lower(),
                          "personalization": it.get("personalization") or ""})
        nxt = data.get("next_starting_after")
        if not nxt or len(items) < 100:
            break
        starting_after = nxt
    return leads


def patch_lead(lead_id, payload):
    resp = requests.patch(f"{INSTANTLY_BASE}/leads/{lead_id}", headers=headers(), json=payload, timeout=30)
    return resp.status_code in (200, 204), resp.status_code, resp.text[:200]


def patch_campaign_sequence(campaign_id):
    sequences = [{"steps": [
        {"type": "email", "delay": 2, "delay_unit": "days", "pre_delay_unit": "days",
         "variants": [{"subject": SUBJECT, "body": "<div>{{personalization}}</div>"}]},
        {"type": "email", "delay": 3, "delay_unit": "days", "pre_delay_unit": "days",
         "variants": [{"subject": "", "body": FU1}]},
        {"type": "email", "delay": 4, "delay_unit": "days", "pre_delay_unit": "days",
         "variants": [{"subject": "", "body": FU2}]},
    ]}]
    resp = requests.patch(f"{INSTANTLY_BASE}/campaigns/{campaign_id}",
                          headers=headers(), json={"sequences": sequences}, timeout=30)
    return resp.status_code in (200, 204), resp.status_code, resp.text[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--campaign_id", required=True)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--sequence_only", action="store_true",
                    help="Only patch the campaign FU1/FU2 steps to 'Hi'; leave sheet + existing leads untouched")
    args = ap.parse_args()

    if not INSTANTLY_API_KEY:
        print("ERROR: INSTANTLY_API_KEY not set"); return

    if args.sequence_only:
        ok, status, text = patch_campaign_sequence(args.campaign_id)
        print(f"Campaign sequence patch (FU1/FU2 -> Hi): {'OK' if ok else 'FAIL'} ({status}) {'' if ok else text}")
        return

    service = get_service()
    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:V"
    ).execute().get("values", [])[1:]

    # ---- Build email -> new_body, and sheet updates -------------------------
    email_to_body = {}
    sheet_updates = []       # {row, body}
    already_hi = 0
    for i, row in enumerate(rows):
        if cell(row, COL_EMAIL_STATUS).lower() != "found":
            continue
        body = cell(row, COL_BODY)
        if not body:
            continue
        email = cell(row, COL_DM_EMAIL).lower()
        if body.startswith(OLD_GREETING):
            new_body = body.replace(OLD_GREETING, NEW_GREETING, 1)
            sheet_updates.append({"row": i + 2, "body": new_body})
        elif body.startswith(NEW_GREETING):
            new_body = body
            already_hi += 1
        else:
            new_body = body  # unexpected greeting; leave as-is but still map for lead sync
        if email:
            email_to_body[email] = new_body

    print(f"=== Patch greeting Hey -> Hi — tab '{tab}' ===")
    print(f"Sheet rows to rewrite (col V): {len(sheet_updates)} | already 'Hi': {already_hi}")

    # sample
    for u in sheet_updates[:3]:
        m = re.match(r"<div>(.*?)</div>", u["body"])
        print(f"  row {u['row']}: greeting -> {m.group(1) if m else u['body'][:40]}")

    if args.dry_run:
        print("\n[DRY RUN] No sheet writes, no campaign/lead patches.")
        # still show what the campaign patch + lead sync would touch
        leads = list_leads(args.campaign_id)
        to_patch = sum(1 for l in leads if l["personalization"].startswith(OLD_GREETING))
        print(f"Campaign leads total: {len(leads)} | leads whose body starts 'Hey': {to_patch}")
        print("Would PATCH campaign sequence (FU1/FU2 -> Hi).")
        return

    # ---- 1. Write col V in batches of 10 -----------------------------------
    for b in range(0, len(sheet_updates), WRITE_BATCH):
        chunk = sheet_updates[b:b + WRITE_BATCH]
        data = [{"range": f"'{tab}'!{col_letter(COL_BODY)}{u['row']}", "values": [[u["body"]]]} for u in chunk]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
        print(f"  col V: wrote {b + len(chunk)}/{len(sheet_updates)}", flush=True)
        time.sleep(0.4)

    # ---- 2. Patch campaign sequence (FU1/FU2) ------------------------------
    ok, status, text = patch_campaign_sequence(args.campaign_id)
    print(f"\nCampaign sequence patch: {'OK' if ok else 'FAIL'} ({status}) {'' if ok else text}")

    # ---- 3. Patch each lead's personalization ------------------------------
    leads = list_leads(args.campaign_id)
    print(f"\nCampaign leads: {len(leads)}")
    patched, skipped, failed = 0, 0, 0
    for idx, l in enumerate(leads):
        cur = l["personalization"]
        if not cur.startswith(OLD_GREETING):
            skipped += 1
            continue
        new_body = email_to_body.get(l["email"], cur.replace(OLD_GREETING, NEW_GREETING, 1))
        ok, st, tx = patch_lead(l["id"], {"personalization": new_body})
        if ok:
            patched += 1
        else:
            failed += 1
            print(f"  FAIL lead {l['email']} ({st}): {tx}")
        if (idx + 1) % 25 == 0:
            print(f"  leads: {idx + 1}/{len(leads)} ({patched} patched, {skipped} skipped)", flush=True)
        time.sleep(0.15)

    print(f"\n=== Done ===")
    print(f"  col V rewritten:   {len(sheet_updates)}")
    print(f"  leads patched:     {patched}")
    print(f"  leads skipped(Hi): {skipped}")
    print(f"  leads failed:      {failed}")


if __name__ == "__main__":
    main()
