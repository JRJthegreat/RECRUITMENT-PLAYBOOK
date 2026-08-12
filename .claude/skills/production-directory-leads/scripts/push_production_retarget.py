"""
Phase 5 - push the production-house icebreaker retarget campaign to
Instantly as a DRAFT. Jude activates manually (repo standing rule).

Saad's 4-email framework (Day 0/2/3/4), sign-offs removed per Jude
(2026-08-12) and per the standing "no sign-off in copy" rule (2026-08-05):
account signature carries identity, nothing in the body or sequence does.

  Step 1 (Day 0)  subject "Production work"   body = {{personalization}}
                  (col AH, generate_production_body.py: greeting +
                  icebreaker + Saad's fixed Email-1 copy)
  Step 2 (Day 2)  blank subject  "Hey {{firstName}} — do you have capacity
                  for new projects right now?"
  Step 3 (Day 3)  blank subject  "Leaving the door open. When your calendar
                  has room, I'm one reply away."
  Step 4 (Day 4)  blank subject  "Door stays open."

Steps 2-4 have no per-lead body (Saad's copy is generic follow-up) and use
Instantly's {{firstName}} merge tag — the lead's first_name is stamped with
their REAL first name (proper_first, shared with generate_production_body.py:
title-cases scraped all-caps names, otherwise passes through as-is), matching
Email 1's greeting. No nickname casualization on recipients in this lane
(Jude, 2026-08-12: people don't like a stranger's cold email renaming them) —
company-name casualization stays, since that's baked into the icebreaker
lines themselves, not the greeting.

Rules honored: DRAFT campaign (never activated here), text_only +
first_email_text_only, NO sending accounts, one lead per unique email (first
row wins, duplicates marked DUP and skipped), personalization sent as a
top-level lead field (never nested under custom_variables — that silently
drops on Instantly's v2 API), blocklist rejections marked BLOCKLISTED and
never retried, batch-of-10 sheet marks, resume-safe (--col_added).

Pushes every row with a valid email AND a body. That now means the full 312-
lead pool: 183 with a researched icebreaker (generate_production_body.py
weaves it in) plus 129 with no icebreaker (Saad's plain copy alone, no "Love
X..." line) — Jude, 2026-08-12, reversing the original personalization-only
design that dropped icebreaker-less rows entirely.

Run:
  python3 -W ignore push_production_retarget.py --sheet_url "URL" --tab Leads \
    [--campaign_id ID] [--limit N] [--dry_run]
"""
import os
import re
import sys
import json
import time
import argparse
import requests
from collections import defaultdict
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))

from generate_production_body import proper_first, get_service  # noqa: E402

INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
TAB_DEFAULT = "Leads"
CAMPAIGN_NAME = "Production Houses Retarget - Aug 2026"

COL_COMPANY = 10   # K
COL_EMAIL = 22     # W
COL_FIRST = 23     # X
COL_LAST = 24      # Y
COL_ADDED = 26     # AA — "Added to Instantly", base schema convention
COL_BODY = 33      # AH — generate_production_body.py

STEP1 = "<div>{{personalization}}</div>"
STEP2 = "<div>Hey {{firstName}} — do you have capacity for new projects right now?</div>"
STEP3 = "<div>Leaving the door open. When your calendar has room, I'm one reply away.</div>"
STEP4 = "<div>Door stays open.</div>"


def col_letter(idx):
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def headers():
    return {"Authorization": f"Bearer {os.getenv('INSTANTLY_API_KEY')}",
            "Content-Type": "application/json"}


def create_campaign():
    payload = {
        "name": CAMPAIGN_NAME,
        "campaign_schedule": {"schedules": [{
            "name": "New schedule",
            "timing": {"from": "09:00", "to": "18:00"},
            "days": {"1": True, "2": True, "3": True, "4": True, "5": True},
            "timezone": "America/Chicago",
        }]},
        "sequences": [{"steps": [
            {"type": "email", "delay": 0, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "Production work", "body": STEP1}]},
            {"type": "email", "delay": 2, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "", "body": STEP2}]},
            {"type": "email", "delay": 1, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "", "body": STEP3}]},
            {"type": "email", "delay": 1, "delay_unit": "days", "pre_delay_unit": "days",
             "variants": [{"subject": "", "body": STEP4}]},
        ]}],
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
    r = requests.post(f"{INSTANTLY_BASE}/campaigns", headers=headers(),
                      json=payload, timeout=30)
    if r.status_code != 200:
        print(f"campaign create failed {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    return r.json()["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default=TAB_DEFAULT)
    ap.add_argument("--campaign_id", default="", help="reuse an existing DRAFT campaign")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    last_col = col_letter(COL_BODY)
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A2:{last_col}").execute().get("values", [])

    def c(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""

    groups = defaultdict(list)  # email -> [(row_num, row)]
    for n, r in enumerate(values, start=2):
        em = c(r, COL_EMAIL).lower()
        if em and c(r, COL_BODY):
            groups[em].append((n, r))

    leads, dup_marks = [], []
    for em, rows in groups.items():
        rows.sort()
        if any(c(rr, COL_ADDED).upper() in ("TRUE", "BLOCKLISTED") for _, rr in rows):
            continue  # resume-safe: inbox already handled
        n0, r0 = rows[0]
        dup_marks += [(n, "DUP") for n, rr in rows[1:] if not c(rr, COL_ADDED)]
        first_raw = c(r0, COL_FIRST)
        leads.append((n0, {
            "email": em,
            "first_name": proper_first(first_raw),
            "last_name": c(r0, COL_LAST),
            "company_name": c(r0, COL_COMPANY),
            "personalization": c(r0, COL_BODY),
        }))

    if args.limit:
        leads = leads[:args.limit]

    print(f"[push] {len(leads)} unique leads | {len(dup_marks)} dup rows"
          f"{' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run:
        for _, l in leads[:5]:
            print(f"  {l['email']:44s} first_name={l['first_name']!r}  company={l['company_name']}")
            print(f"    {l['personalization'][:200]}")
        return

    if not leads:
        return

    cid = args.campaign_id or create_campaign()
    print(f"[push] campaign {cid} (DRAFT — not activated)")

    marks, pushed, blocked = list(dup_marks), 0, 0
    for i, (n0, lead) in enumerate(leads):
        lead["campaign"] = cid
        try:
            r = requests.post(f"{INSTANTLY_BASE}/leads", headers=headers(),
                              json=lead, timeout=30)
            ok, text = r.status_code == 200, r.text[:300]
        except requests.exceptions.RequestException as e:
            ok, text = False, str(e)
        if ok:
            pushed += 1
            marks.append((n0, "TRUE"))
            if pushed == 1:  # verify personalization persisted (standing rule)
                lid = r.json().get("id")
                g = requests.get(f"{INSTANTLY_BASE}/leads/{lid}", headers=headers(),
                                 timeout=30).json()
                have = "personalization" in json.dumps(g) and g.get("payload", {}).get("personalization")
                print(f"  personalization persistence check on {lead['email']}: "
                      f"{'OK' if have else 'MISSING — STOPPING'}")
                if not have:
                    sys.exit(1)
        elif "block" in (text or "").lower():
            blocked += 1
            marks.append((n0, "BLOCKLISTED"))
        else:
            print(f"  FAIL {lead['email']}: {text}")
        if len(marks) >= 10:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{args.tab}'!{col_letter(COL_ADDED)}{rn}", "values": [[v]]}
                    for rn, v in marks]}).execute()
            marks = []
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(leads)} | pushed {pushed} | blocklisted {blocked}", flush=True)
        time.sleep(0.3)
    if marks:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
                {"range": f"'{args.tab}'!{col_letter(COL_ADDED)}{rn}", "values": [[v]]}
                for rn, v in marks]}).execute()

    print(f"\n[push] done: {pushed} pushed, {blocked} blocklisted, campaign {cid} "
          f"is DRAFT — review and activate in Instantly")


if __name__ == "__main__":
    main()
