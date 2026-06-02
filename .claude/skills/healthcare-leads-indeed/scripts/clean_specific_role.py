"""
Extract a specialty-specific clinical role from each messy job title and write it
to col AB "Role Type" on an opening tab.

For each unique job title (col B), GPT-4.1 returns {specialty, credential}:
  - specialty: concise 1-2 word clinical area (oncology, pediatric, psychiatric,
    family, dermatology, wound care, pain management, cardiology, women's health,
    integrative medicine, hospice, urgent care, internal medicine...) or "" if none.
  - credential: "NP" or "PA". NP/PA combos => NP. Never "Physician".
The combined first-mention role (e.g. "oncology NP", "dermatology PA", "NP") is
written to col AB. Specialty + credential are recoverable by splitting on the last space.

Usage:
  python3 -W ignore clean_specific_role.py --sheet_url "URL" --tab "Single Opening" [--preview 15]
"""

import os
import json
import time
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI

from pull_dataset import get_google_service, get_sheet_id_from_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

COL_JOB_TITLE = 1   # B
COL_ROLE      = 27  # AB
LLM_WORKERS   = 10

SYSTEM = """You extract the clinical role from a messy healthcare job title for use in a cold email.

Return ONLY JSON: {"specialty": "<1-2 word clinical area or empty>", "credential": "NP|PA"}

Rules:
- credential: "NP" for nurse practitioner roles, "PA" for physician assistant roles.
  If the title lists BOTH (e.g. "NP or PA", "Nurse Practitioner/Physician Assistant"), return "NP".
  NEVER return "Physician" — a Physician Assistant is "PA".
- specialty: a short, natural clinical area when the title clearly implies one. Examples:
  "Oncology Nurse Practitioner" -> oncology
  "Psychiatric Mental Health Nurse Practitioner" / "PMHNP" -> psychiatric
  "Family Nurse Practitioner" / "FNP" -> family
  "Nurse Practitioner - Dermatology" -> dermatology
  "FNP Wound Care" -> wound care
  "Pediatric Nurse Practitioner" -> pediatric
  "Women's Health Nurse Practitioner" / "WHNP" -> women's health
  "Cardiology APP ..." -> cardiology
  "Nurse Practitioner/Physician Assistant - Pain Management" -> pain management
  Keep it 1-2 words, lowercase. Use "" (empty) when the title has no clear specialty
  (e.g. plain "Nurse Practitioner", "Advanced Practice Provider", "Nurse Practitioner West Loop").
- Ignore and strip: city/state, employment type (part-time, full-time, PRN, per diem, weekends),
  seniority/fellowship/new grad, facility or brand names, neighborhoods, requisition IDs,
  "Copy of", "Needed", shift details, marketing fluff.
- Do not invent a specialty that is not implied by the title."""


def safe_get(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_gid(svc, sid, tab):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"], s["properties"]["gridProperties"]["rowCount"]
    return None, None


def extract_role(client, title):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=40, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f'Job title: "{title}"'},
            ],
        )
        d = json.loads(resp.choices[0].message.content or "{}")
        spec = (d.get("specialty") or "").strip().lower()
        cred = (d.get("credential") or "NP").strip().upper()
        if cred not in ("NP", "PA"):
            cred = "NP"
        # safety: collapse junk specialties
        if spec in ("none", "n/a", "general", "advanced practice"):
            spec = ""
        return f"{spec} {cred}".strip() if spec else cred
    except Exception:
        return "NP"


def main():
    ap = argparse.ArgumentParser(description="Extract specialty-specific role to col AB")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    svc = get_google_service()
    sid = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)

    print(f"=== Clean Specific Role ({'PREVIEW' if args.preview else 'LIVE'}) — {args.tab} ===\n")

    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A2:AB5000"
    ).execute().get("values", [])

    # unique titles -> sheet rows
    titles = OrderedDict()
    for i, r in enumerate(rows):
        t = safe_get(r, COL_JOB_TITLE)
        if not t:
            continue
        titles.setdefault(t, []).append(i + 2)

    print(f"Unique job titles: {len(titles)}")

    if args.preview:
        sample = list(titles.keys())[:args.preview]
        for t in sample:
            print(f"  {t[:55]:55s} -> {extract_role(client, t)}")
        print("\n[PREVIEW] No writes.")
        return

    # header
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
            {"range": f"'{args.tab}'!{col_letter(COL_ROLE)}1", "values": [["Role Type"]]}
        ]}).execute()

    def run(t):
        return t, extract_role(client, t)

    pending, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run, t): t for t in titles}
        for fut in as_completed(futs):
            t, role = fut.result()
            done += 1
            for rn in titles[t]:
                pending.append({"range": f"'{args.tab}'!{col_letter(COL_ROLE)}{rn}", "values": [[role]]})
            if len(pending) >= 200 or done == len(titles):
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sid, body={"valueInputOption": "RAW", "data": pending}).execute()
                pending = []
                time.sleep(0.3)
            if done % 50 == 0:
                print(f"  {done}/{len(titles)} done")

    # 18px row height
    gid, rc = get_gid(svc, sid, args.tab)
    if gid is not None:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": rc},
            "properties": {"pixelSize": 18}, "fields": "pixelSize"}}]}).execute()

    print(f"\nDone. Specific role written to col {col_letter(COL_ROLE)} for {len(titles)} titles.")


if __name__ == "__main__":
    main()
