"""
One-off sync: push updated Email 1 personalization to leads already loaded
into the production-house DRAFT campaign, after the icebreaker tone-revision
pass (Jude, 2026-08-12 — fixing analytical/essay-like compliment clauses like
"says a lot about" and "a specific kind of X sensibility to nail").

Only the rows whose icebreaker text actually changed need a re-sync; the
rest were already pushed with correct copy. Takes --rows (JSON file, list of
sheet row numbers) and PATCHes each corresponding Instantly lead's
personalization field with the current sheet value (col AH).

Campaign is still DRAFT (nothing sent), so this is safe — no risk of
updating copy on an email that already went out.

Run:
  python3 -W ignore sync_revised_personalization.py --sheet_url "URL" --tab Leads \
    --campaign_id ID --rows data/changed_rows.json [--dry_run]
"""
import os
import re
import sys
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

COL_EMAIL = 22  # W
COL_BODY = 33   # AH


def headers():
    return {"Authorization": f"Bearer {os.getenv('INSTANTLY_API_KEY')}",
            "Content-Type": "application/json"}


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


def list_leads_in_campaign(campaign_id):
    leads, starting_after = [], None
    while True:
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        r = requests.post(f"{INSTANTLY_BASE}/leads/list", headers=headers(), json=body, timeout=30)
        if r.status_code not in (200, 201):
            print(f"  list leads failed: {r.status_code} {r.text[:200]}")
            break
        data = r.json()
        items = data.get("items", [])
        if not items:
            break
        leads.extend(items)
        starting_after = data.get("next_starting_after")
        if not starting_after:
            break
    return leads


def patch_personalization(lead_id, body):
    r = requests.patch(f"{INSTANTLY_BASE}/leads/{lead_id}", headers=headers(),
                       json={"personalization": body}, timeout=30)
    return r.status_code in (200, 201), r.text[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--campaign_id", required=True)
    ap.add_argument("--rows", required=True, help="JSON file: list of sheet row numbers to sync")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    with open(args.rows) as f:
        target_rows = set(json.load(f))

    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
    service = get_service()
    last_col_letter = "AH"
    values = service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A2:{last_col_letter}").execute().get("values", [])

    def c(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""

    to_sync = []
    for i, r in enumerate(values):
        row = i + 2
        if row not in target_rows:
            continue
        email, body = c(r, COL_EMAIL), c(r, COL_BODY)
        if email and body:
            to_sync.append((row, email.lower(), body))

    print(f"[sync] {len(to_sync)}/{len(target_rows)} target rows have email+body")

    print("[sync] listing leads in campaign...")
    leads = list_leads_in_campaign(args.campaign_id)
    by_email = {(l.get("email") or "").lower(): l.get("id") for l in leads}
    print(f"[sync] {len(leads)} leads in campaign, {len(by_email)} unique emails")

    missing = [em for _, em, _ in to_sync if em not in by_email]
    if missing:
        print(f"[sync] WARNING: {len(missing)} target emails not found in campaign: {missing[:10]}")

    if args.dry_run:
        for row, em, body in to_sync[:5]:
            print(f"  row {row} | {em} | id={by_email.get(em, 'MISSING')}")
            print(f"    {body[:150]}")
        return

    ok, fail = 0, 0
    for row, em, body in to_sync:
        lid = by_email.get(em)
        if not lid:
            fail += 1
            continue
        success, msg = patch_personalization(lid, body)
        if success:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL row {row} {em}: {msg}")
        time.sleep(0.2)

    print(f"\n[sync] done: {ok} patched, {fail} failed/missing")


if __name__ == "__main__":
    main()
