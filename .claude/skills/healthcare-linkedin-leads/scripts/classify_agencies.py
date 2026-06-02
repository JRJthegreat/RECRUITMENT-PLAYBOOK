"""
Classify each unique company on the external LinkedIn-jobs sheet as:
  - staffing_agency : recruits/places candidates at OTHER employers (locum, travel,
                      perm placement, RPO; "on behalf of our client")
  - job_board       : a job aggregator / marketplace listing other companies' jobs
  - direct_employer : the practice / clinic / health system / clinician network that
                      actually employs the hire directly

NOTE: national telehealth / therapy networks that directly contract their own
clinicians (Headway, SonderMind, Talkiatry, etc.) are direct_employer, NOT agencies.

Signals used: company name, LinkedIn company description, website domain, a sample
job description. Does NOT use the ATS column. Does NOT use opening-count.

Safe label-then-delete workflow (avoids LLM nondeterminism between count and delete):
  1. report (default)   : classify in memory, print counts. Writes nothing.
  2. --write_labels      : classify + freeze label/reason into cols X/Y. Resume-safe
                           (skips companies already labeled; --reclassify to override).
  3. --apply             : delete agency/job_board rows by READING the frozen col-X
                           labels. No LLM call, fully deterministic.

Run --write_labels once, eyeball cols X/Y in the sheet, then --apply.

Usage:
  python3 -W ignore classify_agencies.py --sheet_url "URL"                 # report
  python3 -W ignore classify_agencies.py --sheet_url "URL" --write_labels  # freeze labels
  python3 -W ignore classify_agencies.py --sheet_url "URL" --apply         # delete from labels
"""

import os
import re
import json
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI

from pull_dataset import get_google_service, get_sheet_id_from_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

# External sheet columns (0-based)
COL_TITLE = 0        # A
COL_JOB_DESC = 1     # B
COL_COMPANY_NAME = 12  # M
COL_ABOUT_LINK = 19  # T
COL_WEBSITE = 20     # U
COL_COMPANY_DESC = 22  # W
# Frozen-label columns written by --write_labels (audit trail; deletion reads these)
COL_LABEL = 23         # X — Company Type
COL_LABEL_REASON = 24  # Y — Type Reason
LABEL_HEADER = "Company Type"
REASON_HEADER = "Type Reason"
DROP_LABELS = {"staffing_agency", "job_board"}

LLM_WORKERS = 8


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result

CLASSIFY_SYSTEM = """You classify a company that posted a clinical job (Nurse Practitioner / PA / Physician) on LinkedIn. Decide whether the company is the DIRECT EMPLOYER of the hire, or instead a STAFFING AGENCY or a JOB BOARD that is not the real employer.

Definitions:
- staffing_agency: A recruitment / staffing / locum tenens / travel-nursing / RPO firm that sources and places clinicians at OTHER employers. It is hiring to put the person on a client's site or its own bench, not to work at the firm itself. Signals: name contains Staffing / Recruiting / Recruitment / Locums / Talent / Workforce / Search / Solutions; description says "we connect/place talent", "our clients", "on behalf of our client", "staffing solutions", "we recruit for". Brand-name agencies with no giveaway word still count (e.g. Jobot, gpac, Medix, Yoh, Aya, Nurse Avenue).
- job_board: A job aggregator / marketplace / listing platform that republishes other companies' jobs. The hiring entity is not the platform.
- direct_employer: The actual practice, clinic, medical group, hospital/health system, med spa, or clinician network that directly employs or contracts the hire to deliver care under its own brand. National telehealth / therapy networks that directly contract their own clinicians (e.g. Headway, SonderMind, Talkiatry, Grow Therapy, Thriveworks) are direct_employer, NOT agencies.

Use the company name, company description, website domain, and the sample job description together. The job description giving away "our client" / "we are recruiting on behalf of" => staffing_agency.

Return ONLY valid JSON: {"classification": "direct_employer|staffing_agency|job_board", "reason": "<one short sentence>"}"""

CLASSIFY_USER_TEMPLATE = """Company name: {company}
Website: {website}
{company_desc_block}Sample job title: {job_title}
Sample job description (first 700 chars):
{job_desc}

Classify per the rules. Return JSON only."""


def safe_get(row, idx):
    return row[idx].strip() if len(row) > idx and row[idx] else ""


def normalize_company_url(u):
    u = (u or "").strip().rstrip("/")
    if u.lower().endswith("/about"):
        u = u[:-len("/about")]
    return u.lower()


def get_gid_from_url(url):
    m = re.search(r"[#&?]gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    """Return (title, numeric_sheet_id) for the gid in the URL (or first tab)."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"], s["properties"]["sheetId"]
    s = meta["sheets"][0]
    return s["properties"]["title"], s["properties"]["sheetId"]


def classify_one(client, company, website, job_title, job_desc, company_desc):
    company_desc_block = f"Company description: {company_desc[:500]}\n" if company_desc else ""
    job_desc_truncated = job_desc[:700] if job_desc else "(not available)"
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": CLASSIFY_USER_TEMPLATE.format(
                    company=company, website=website or "(unknown)",
                    company_desc_block=company_desc_block,
                    job_title=job_title or "(unknown)",
                    job_desc=job_desc_truncated,
                )},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"classification": "direct_employer", "reason": "no JSON (defaulted)"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"classification": "direct_employer", "reason": f"error: {e}"}


def main():
    parser = argparse.ArgumentParser(description="Count staffing agencies / job boards on the LinkedIn jobs sheet (read-only)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=0, help="Only classify first N companies (debug)")
    parser.add_argument("--workers", type=int, default=LLM_WORKERS)
    parser.add_argument("--write_labels", action="store_true",
                        help="Classify + write Company Type/Reason to cols X/Y (resume-safe, non-destructive)")
    parser.add_argument("--apply", action="store_true",
                        help="Delete agency/job_board rows by reading the FROZEN col-X labels (no LLM)")
    parser.add_argument("--reclassify", action="store_true",
                        help="With --write_labels: re-classify even companies that already have a label")
    args = parser.parse_args()

    if args.write_labels and args.apply:
        print("ERROR: run --write_labels first, review the sheet, then run --apply separately.")
        return

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab, tab_sheet_id = resolve_tab(service, sheet_id, args.sheet_url)

    if args.apply:
        mode = "APPLY — delete from frozen labels (no LLM)"
    elif args.write_labels:
        mode = "WRITE LABELS — classify + write cols X/Y"
    else:
        mode = "READ-ONLY report"
    print(f"=== Classify Agencies / Job Boards ({mode}) ===")
    print(f"Tab:   {tab!r}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A2:Y10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    # Group by company key (normalized LinkedIn URL, fallback name)
    groups = OrderedDict()
    for i, r in enumerate(rows):
        sheet_row = i + 2  # 1-based, +1 for header
        name = safe_get(r, COL_COMPANY_NAME)
        if not name:
            continue
        key = normalize_company_url(safe_get(r, COL_ABOUT_LINK)) or name.lower()
        if key not in groups:
            groups[key] = {
                "name": name,
                "website": safe_get(r, COL_WEBSITE),
                "company_desc": safe_get(r, COL_COMPANY_DESC),
                "job_title": safe_get(r, COL_TITLE),
                "job_desc": safe_get(r, COL_JOB_DESC),
                "existing_label": safe_get(r, COL_LABEL),
                "rows": 0,
                "row_nums": [],
            }
        groups[key]["rows"] += 1
        groups[key]["row_nums"].append(sheet_row)
        if not groups[key]["company_desc"]:
            groups[key]["company_desc"] = safe_get(r, COL_COMPANY_DESC)

    # ---- APPLY: delete from frozen col-X labels, no LLM ----
    if args.apply:
        rows_to_delete = []
        unlabeled = 0
        for key, d in groups.items():
            label = d["existing_label"].lower()
            if not label:
                unlabeled += d["rows"]
                continue
            if label in DROP_LABELS:
                for rn in d["row_nums"]:
                    rows_to_delete.append((rn, d["name"], label))
        if unlabeled:
            print(f"[!] {unlabeled} rows have no label in col X — run --write_labels first. They will NOT be touched.")
        if not rows_to_delete:
            print("Nothing labeled agency/job_board to delete.")
            return
        print(f"\nDeleting {len(rows_to_delete)} rows (bottom-up) from frozen labels...")
        requests_body = []
        for rn, name, cat in sorted(rows_to_delete, key=lambda x: -x[0]):
            print(f"  delete row {rn:4d}  {cat:15s} {name[:40]}")
            requests_body.append({"deleteDimension": {"range": {
                "sheetId": tab_sheet_id, "dimension": "ROWS",
                "startIndex": rn - 1, "endIndex": rn}}})
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests_body}).execute()
        print(f"\nDeleted {len(rows_to_delete)} rows. Remaining data rows: {len(rows) - len(rows_to_delete)}")
        return

    # ---- classify (for report or write_labels) ----
    keys = list(groups.keys())
    if args.limit:
        keys = keys[:args.limit]

    # Resume-safe: in write_labels mode, skip companies already labeled (unless --reclassify)
    to_classify = keys
    if args.write_labels and not args.reclassify:
        to_classify = [k for k in keys if not groups[k]["existing_label"]]
        print(f"Unique companies: {len(keys)}  (already labeled: {len(keys) - len(to_classify)}, to classify: {len(to_classify)})\n")
    else:
        print(f"Unique companies: {len(keys)}\n")

    results = {}
    # Seed with existing labels so the report/write reflects the full picture
    for k in keys:
        if groups[k]["existing_label"]:
            results[k] = {"classification": groups[k]["existing_label"], "reason": "(existing label)"}

    if to_classify:
        print(f"Classifying {len(to_classify)} with {AZURE_DEPLOYMENT} ({args.workers} workers)...")
        client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)

        def run(key):
            d = groups[key]
            return key, classify_one(client, d["name"], d["website"], d["job_title"], d["job_desc"], d["company_desc"])

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run, k): k for k in to_classify}
            for fut in as_completed(futs):
                key, res = fut.result()
                results[key] = res
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(to_classify)}")

    # Tally
    cat_companies = {"staffing_agency": 0, "job_board": 0, "direct_employer": 0}
    cat_rows = {"staffing_agency": 0, "job_board": 0, "direct_employer": 0}
    flagged = []
    for key in keys:
        c = results.get(key, {}).get("classification", "direct_employer")
        if c not in cat_companies:
            c = "direct_employer"
        cat_companies[c] += 1
        cat_rows[c] += groups[key]["rows"]
        if c in DROP_LABELS:
            flagged.append((groups[key]["name"], groups[key]["rows"], c, results.get(key, {}).get("reason", "")))

    print("\n" + "=" * 90)
    print("RESULTS")
    print("=" * 90)
    print(f"{'category':18s} {'companies':>10s} {'job rows':>10s}")
    for c in ("staffing_agency", "job_board", "direct_employer"):
        print(f"{c:18s} {cat_companies[c]:>10d} {cat_rows[c]:>10d}")
    drop_co = cat_companies["staffing_agency"] + cat_companies["job_board"]
    drop_rows = cat_rows["staffing_agency"] + cat_rows["job_board"]
    print("-" * 40)
    print(f"{'TO FILTER OUT':18s} {drop_co:>10d} {drop_rows:>10d}")
    print(f"{'KEEP (employers)':18s} {cat_companies['direct_employer']:>10d} {cat_rows['direct_employer']:>10d}")

    print(f"\nFlagged agencies / job boards ({len(flagged)} companies, sorted by rows):")
    for name, nrows, cat, reason in sorted(flagged, key=lambda x: -x[1]):
        print(f"  [{nrows:2d}x] {cat:15s} {name[:38]:38s} — {reason}")

    if not args.write_labels:
        print(f"\n[READ-ONLY] No changes. Run --write_labels to freeze these labels into cols X/Y,")
        print(f"            then review the sheet and run --apply to delete the flagged rows.")
        return

    # ---- write labels to cols X/Y for every row of each classified company ----
    updates = [
        {"range": f"'{tab}'!{col_letter(COL_LABEL)}1", "values": [[LABEL_HEADER]]},
        {"range": f"'{tab}'!{col_letter(COL_LABEL_REASON)}1", "values": [[REASON_HEADER]]},
    ]
    for key in to_classify:
        res = results.get(key, {})
        label = res.get("classification", "direct_employer")
        reason = res.get("reason", "")[:300]
        for rn in groups[key]["row_nums"]:
            updates.append({"range": f"'{tab}'!{col_letter(COL_LABEL)}{rn}", "values": [[label]]})
            updates.append({"range": f"'{tab}'!{col_letter(COL_LABEL_REASON)}{rn}", "values": [[reason]]})

    for i in range(0, len(updates), 500):
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": updates[i:i + 500]}
        ).execute()
    print(f"\nWrote labels for {len(to_classify)} companies to cols "
          f"{col_letter(COL_LABEL)}/{col_letter(COL_LABEL_REASON)}. "
          f"Review the sheet, then run with --apply to delete agency/job_board rows.")


if __name__ == "__main__":
    main()
