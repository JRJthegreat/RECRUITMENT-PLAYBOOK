"""
Phase 4 - assemble Email 1's body: greeting + icebreaker + Saad's fixed
Email 1 copy. NO LLM call — Saad's template has no {icp}/{roles} slots to
extract (unlike the healthcare/recruitment templates this pipeline is
patterned after), so this is pure string assembly.

Emails 2-4 of the sequence carry no personalization (Saad's copy is generic
follow-up: "do you have capacity", "leaving the door open", "door stays
open") and live directly in push_production_retarget.py's sequence
definition, using Instantly's {{firstName}} merge tag rather than a
generated body — there is nothing per-lead to write for them.

Sign-offs removed per Jude's instruction (2026-08-12): no "Best," block.
No nickname casualization on the recipient's own name in this lane (Jude,
2026-08-12: cold-email recipients don't like being renamed by a stranger) —
proper_first() only fixes scraped all-caps names, it never substitutes a
nickname. push_production_retarget.py uses the same proper_first() for the
lead's stored first_name, so {{firstName}} in steps 2-4 matches Email 1's
greeting exactly. Company-name casualization is unaffected — it's already
baked into the icebreaker text upstream, not the greeting.

Writes --col_body (Email 1 full plain-text body, rides to Instantly as
{{personalization}}). Batch-of-10, resume-safe: skips rows where --col_body
is filled. Runs on every row with a valid email (col W), icebreaker or not
(Jude, 2026-08-12: send the plain copy to leads with no researched
icebreaker rather than drop them — reversing the original personalization-
only design). A row WITH an icebreaker (col AF) gets it woven in; a row
without just gets Saad's fixed copy on its own.

Run:
  python3 -W ignore generate_production_body.py --sheet_url "URL" --tab Leads \
    [--limit N] [--preview N]
"""
import os
import re
import json
import time
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

WRITE_BATCH = 10

COL_EMAIL = 22         # W
COL_FIRST = 23         # X
COL_ICEBREAKER = 31    # AF (apply_icebreakers.py)
COL_BODY = 33          # AH — Email 1 body

# ---------------------------------------------------------------------------
# Saad's Email 1 copy, minus the sign-off (removed per Jude, 2026-08-12) and
# minus "Worth a quick call?" (removed per Jude, 2026-08-12 — not pushing for
# a call this early in the sequence). Only the greeting name is a variable.
# ---------------------------------------------------------------------------
BODY_CORE = (
    "I have brands actively looking for commercial production right now.\n\n"
    "Before I route anyone anywhere, wanted to check if you have capacity."
)

# Jude, 2026-08-12: no nickname casualization on the RECIPIENT's own name for
# this lane — people don't like being renamed by a stranger's cold email.
# Company-name casualization stays (already baked into the icebreaker lines
# by apply_icebreakers.py's upstream research pack, e.g. "asv has been...").
def proper_first(name):
    n = (name or "").strip().split()[0] if (name or "").strip() else ""
    return n.title() if n and n == n.lower() else n


def col_letter(idx):
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


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


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def assemble(first, icebreaker):
    parts = [f"Hi {proper_first(first)},", ""]
    if icebreaker:
        parts += [icebreaker, ""]
    parts.append(BODY_CORE)
    return "\n".join(parts).replace("—", ", ").replace("–", ", ")


def ensure_col(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    need = COL_BODY + 1
    current = sheet["properties"]["gridProperties"]["columnCount"]
    if current < need:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": need - current}}]}).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!{col_letter(COL_BODY)}1",
        valueInputOption="RAW", body={"values": [["email_body"]]}).execute()


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(COL_BODY)}{r}", "values": [[v]]} for r, v in updates]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
    print(f"  -> wrote {len(updates)} rows", flush=True)
    time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--no_icebreaker", action="store_true",
                    help="Ignore col AF entirely — plain Saad copy for every "
                         "lead (Jude, 2026-08-12: icebreakers dropped campaign-wide)")
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    if not args.preview:
        ensure_col(service, sheet_id, tab)

    last_col = col_letter(COL_BODY)
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last_col}").execute().get("values", [])[1:]

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    pending = []
    for i, r in enumerate(rows):
        email = cell(r, COL_EMAIL)
        existing = cell(r, COL_BODY)
        if not email or existing:
            continue
        ice = "" if args.no_icebreaker else cell(r, COL_ICEBREAKER)
        pending.append({"row": i + 2, "first": cell(r, COL_FIRST), "ice": ice})

    if args.limit:
        pending = pending[:args.limit]

    print(f"=== Generate Production Body (Email 1) ===\nRows to assemble: {len(pending)}\n")

    if args.preview:
        pending = pending[:args.preview]
        for p in pending:
            body = assemble(p["first"], p["ice"])
            print(f"--- Row {p['row']} ---")
            print(body)
            print()
        return

    if not pending:
        return

    updates = []
    for p in pending:
        body = assemble(p["first"], p["ice"])
        updates.append((p["row"], body))
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab)
            updates = []
    if updates:
        flush(service, updates, sheet_id, tab)

    print(f"\nDone - {len(pending)} bodies written.")


if __name__ == "__main__":
    main()
