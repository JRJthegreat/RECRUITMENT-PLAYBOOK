"""
Phase 5 - push rendered retarget leads to a new DRAFT Instantly campaign.

Campaign is created as DRAFT with NO sending accounts (Jude activates and adds
accounts in the UI). Single-step sequence: subject "new reqs", body is
{{personalization}} = the assembled email from col AB (greeting + icebreaker +
Jude's fixed offer template). Text-only campaign flags set. Follow-up steps are
NOT scripted: their copy was never approved, Jude adds them in the UI.

Lead selection (Cold Pool schema):
  A=email, B/C=names, D=company, AB=body, AC=variant, X=flags, AD=added
  - body present (implies icebreaker present: personalization-only experiment)
  - email present, not a duplicate (within this push or already pushed rows)
  - flags must not contain MOVED (they left the company: email likely dead)
    or NOT_A_RECRUITER
  - added col not TRUE / BLOCKLISTED (blocklist-rejected leads are never retried)

Batch-of-10 sheet marking, resume-safe. --campaign_id resumes into an existing
campaign instead of creating a new one.

Run:
  python3 -W ignore push_retarget.py --sheet_url "URL" --tab "Cold Pool" \
    --campaign_name "Recruitment - Retargeting - Jul 21" [--dry_run] [--limit N]
"""

import os
import re
import json
import time
import socket
import argparse
import requests
from dotenv import load_dotenv

socket.setdefaulttimeout(180)
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")
INSTANTLY_BASE    = "https://api.instantly.ai/api/v2"
BATCH_SIZE = 10

SUBJECT    = "new reqs"
STEP1_BODY = "<div>{{personalization}}</div>"


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell(row, idx):
    return row[idx].strip() if idx is not None and idx < len(row) and row[idx] else ""


def instantly_headers():
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


def create_campaign(name, timezone):
    payload = {
        "name": name,
        "campaign_schedule": {"schedules": [{
            "name": "New schedule",
            "timing": {"from": "09:00", "to": "18:00"},
            "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
            "timezone": timezone,
        }]},
        "sequences": [{"steps": [{
            "type": "email", "delay": 2, "delay_unit": "days", "pre_delay_unit": "days",
            "variants": [{"subject": SUBJECT, "body": STEP1_BODY}],
        }]}],
        "daily_limit": 500,
        "stop_on_reply": True,
        "stop_on_auto_reply": False,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": True,
        "first_email_text_only": True,
        "prioritize_new_leads": False,
        "stop_for_company": False,
    }
    resp = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=instantly_headers(),
                         json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Campaign creation error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()["id"]


def push_lead(campaign_id, lead_payload):
    payload = dict(lead_payload)
    payload["campaign"] = campaign_id
    try:
        resp = requests.post(f"{INSTANTLY_BASE}/leads", headers=instantly_headers(),
                             json=payload, timeout=30)
        return resp.status_code == 200, resp.status_code, resp.text[:300]
    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def looks_blocklisted(status, text):
    return "block" in (text or "").lower()


def ensure_added_col(service, sheet_id, tab_name, c_added):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current = sheet["properties"]["gridProperties"]["columnCount"]
    if current < c_added + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": (c_added + 1) - current,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!{col_letter(c_added)}1",
        valueInputOption="RAW", body={"values": [["added_to_instantly"]]},
    ).execute()


def mark(service, sheet_id, tab_name, c_added, marks):
    """marks: list of (row_num, value)."""
    if not marks:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(c_added)}{r}", "values": [[v]]}
            for r, v in marks]
    for attempt in range(3):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": data}).execute()
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                print(f"  Sheet write failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Cold Pool")
    ap.add_argument("--campaign_name", required=True)
    ap.add_argument("--campaign_id", default="", help="Resume into an existing campaign")
    ap.add_argument("--col_email", default="A")
    ap.add_argument("--col_first", default="B")
    ap.add_argument("--col_last", default="C")
    ap.add_argument("--col_company", default="D")
    ap.add_argument("--col_website", default="F")
    ap.add_argument("--col_source", default="H")
    ap.add_argument("--col_dm_linkedin", default="O")
    ap.add_argument("--col_company_linkedin", default="P")
    ap.add_argument("--col_flags", default="X")
    ap.add_argument("--col_body", default="AB")
    ap.add_argument("--col_variant", default="AC")
    ap.add_argument("--col_added", default="AD")
    ap.add_argument("--timezone", default="America/Detroit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    a = ap.parse_args()

    if not INSTANTLY_API_KEY:
        raise SystemExit("INSTANTLY_API_KEY not set in .env")

    sheet_id = parse_sheet_id(a.sheet_url)
    tab = a.tab
    C = {k: col_to_idx(v) for k, v in {
        "email": a.col_email, "first": a.col_first, "last": a.col_last,
        "company": a.col_company, "website": a.col_website, "source": a.col_source,
        "dm_li": a.col_dm_linkedin, "co_li": a.col_company_linkedin,
        "flags": a.col_flags, "body": a.col_body, "variant": a.col_variant,
        "added": a.col_added,
    }.items()}

    print("=== Push Retarget Leads -> Instantly ===\n")
    service = get_service()
    if not a.dry_run:
        ensure_added_col(service, sheet_id, tab, C["added"])

    last = col_letter(max(C.values()))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last}"
    ).execute().get("values", [])[1:]
    print(f"Total rows: {len(rows)}")

    # every email already marked pushed or blocklisted, for cross-run dedupe
    seen = set()
    for row in rows:
        if cell(row, C["added"]).upper() in ("TRUE", "BLOCKLISTED"):
            e = cell(row, C["email"]).lower()
            if e:
                seen.add(e)

    leads, skipped = [], {"moved": 0, "dupe": 0, "no_body": 0}
    for i, row in enumerate(rows):
        if a.limit and len(leads) >= a.limit:
            break
        if cell(row, C["added"]).upper() in ("TRUE", "BLOCKLISTED"):
            continue
        email = cell(row, C["email"]).lower()
        body  = cell(row, C["body"])
        flags = cell(row, C["flags"])
        if not email or not body:
            skipped["no_body"] += 1
            continue
        if "MOVED" in flags or "NOT_A_RECRUITER" in flags:
            skipped["moved"] += 1
            continue
        if email in seen:
            skipped["dupe"] += 1
            continue
        seen.add(email)
        # Follow-up steps reference {{icp}}; parse it from the body's own
        # tracking line so every lead lands with the variable already set.
        m = re.search(r"I'm tracking (.+?) hiring for", body)
        leads.append({
            "row_num": i + 2,
            "payload": {
                "email":           email,
                "first_name":      cell(row, C["first"]),
                "last_name":       cell(row, C["last"]),
                "company_name":    cell(row, C["company"]),
                "website":         cell(row, C["website"]),
                "personalization": body,
                "custom_variables": {
                    "icp":              m.group(1).strip() if m else "employers",
                    "dm_linkedin":      cell(row, C["dm_li"]),
                    "company_linkedin": cell(row, C["co_li"]),
                    "variant":          cell(row, C["variant"]),
                    "source_campaigns": cell(row, C["source"]),
                },
            },
        })

    print(f"Leads to push: {len(leads)}  (skipped: {skipped})\n")
    if a.dry_run:
        for lead in leads[:5]:
            p = lead["payload"]
            print(f"  Row {lead['row_num']}: {p['first_name']} {p['last_name']} <{p['email']}> ({p['company_name']})")
            print("    " + p["personalization"][:200].replace(chr(10), " / ") + " ...\n")
        print(f"[DRY RUN] Would create DRAFT campaign '{a.campaign_name}' and push {len(leads)} leads.")
        return
    if not leads:
        print("Nothing to push.")
        return

    if a.campaign_id:
        campaign_id = a.campaign_id
        print(f"Resuming into campaign {campaign_id}\n")
    else:
        print(f"Creating DRAFT campaign '{a.campaign_name}'...")
        campaign_id = create_campaign(a.campaign_name, a.timezone)
        print(f"  Campaign created: {campaign_id}\n")

    added, failed = 0, []
    pending = []
    for i, lead in enumerate(leads):
        ok, status, text = push_lead(campaign_id, lead["payload"])
        if ok:
            added += 1
            pending.append((lead["row_num"], "TRUE"))
        elif looks_blocklisted(status, text):
            pending.append((lead["row_num"], "BLOCKLISTED"))
        else:
            print(f"  Lead {i+1} failed ({status}): {text}")
            failed.append(lead)
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(leads):
            mark(service, sheet_id, tab, C["added"], pending)
            pending = []
            print(f"  Progress: {i+1}/{len(leads)} ({added} added, {len(failed)} failed)", flush=True)
            if i + 1 < len(leads):
                time.sleep(1.5)

    if failed:
        print(f"\n  Retrying {len(failed)} failed leads...")
        for attempt in range(1, 4):
            still, recovered = [], []
            time.sleep(5 * attempt)
            for lead in failed:
                ok, status, text = push_lead(campaign_id, lead["payload"])
                if ok:
                    added += 1
                    recovered.append((lead["row_num"], "TRUE"))
                elif looks_blocklisted(status, text):
                    recovered.append((lead["row_num"], "BLOCKLISTED"))
                else:
                    still.append(lead)
            mark(service, sheet_id, tab, C["added"], recovered)
            print(f"  Retry {attempt}: {len(failed) - len(still)} recovered, {len(still)} still failing")
            failed = still
            if not failed:
                break

    print(f"\n=== Done ===")
    print(f"  Pushed:      {added}")
    print(f"  Failed:      {len(failed)}")
    print(f"  Campaign ID: {campaign_id}")
    print(f"\n  DRAFT campaign - activate in the Instantly UI when ready.")
    print(f"  Sending accounts are never added by script; follow-up steps not scripted.")


if __name__ == "__main__":
    main()
