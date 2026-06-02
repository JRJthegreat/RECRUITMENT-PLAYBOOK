"""
Classify each unique company on the LinkedIn-jobs sheet into an EMPLOYER TYPE,
to separate the ICP (independent practices) from platforms/systems/chains.

Categories:
  - independent_practice : ICP. A single private practice, clinic, medical/dental
                           group, med spa, or physician-owned group under its own
                           local brand, one-to-a-few locations, no national footprint
                           or PE/franchise parent.
  - telehealth_platform  : National virtual-care / therapy / telehealth network or
                           marketplace that contracts clinicians at scale (Headway,
                           Talkiatry, Thriveworks, SonderMind, Grow Therapy, Charlie
                           Health, Brightside, Two Chairs). Continuous nationwide
                           clinician recruiting is core to the model.
  - hospital_system      : Hospital, health system, academic medical center, or large
                           integrated delivery network with internal TA (Tenet,
                           CommonSpirit, UNC Health, Northwestern, Baptist Health
                           System, Acadia, regional medical centers).
  - chain_franchise      : Multi-location chain, franchise, or PE-backed group running
                           many sites under one brand with centralized hiring (Peachtree
                           Immediate Care, American Family Care, JumpstartMD,
                           Medi-Weightloss, urgent-care chains, large MSO/PE platforms).

NOTHING IS DELETED. Labels are frozen into cols X (Employer Type) / Y (Type Reason)
for review. The Single/Multiple tabs are later built from independent_practice only.

Safe workflow (mirrors classify_agencies.py):
  python3 -W ignore classify_employer_type.py --sheet_url "URL"                 # report
  python3 -W ignore classify_employer_type.py --sheet_url "URL" --write_labels  # freeze X/Y
  (resume-safe: skips already-labeled companies; --reclassify to override)
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

COL_TITLE = 0          # A
COL_JOB_DESC = 1       # B
COL_COMPANY_NAME = 12  # M
COL_ABOUT_LINK = 19    # T
COL_WEBSITE = 20       # U
COL_COMPANY_DESC = 22  # W
COL_LABEL = 23         # X — Employer Type
COL_LABEL_REASON = 24  # Y — Type Reason
LABEL_HEADER = "Employer Type"
REASON_HEADER = "Type Reason"

CATEGORIES = ["independent_practice", "telehealth_platform", "hospital_system", "chain_franchise"]
LLM_WORKERS = 8

CLASSIFY_SYSTEM = """You classify a healthcare company that posted a clinical job (Nurse Practitioner / PA / Physician). The goal is to separate practices a small-business connector can sell to (the ICP) from platforms, hospital systems, and large chains.

Be GENEROUS toward independent_practice. A privately-owned practice or group is the ICP even if it runs several locations, as long as it operates under its own local/regional brand and is not a franchise, a PE/MSO roll-up, or a large national chain.

Categories:
- independent_practice: A private practice, clinic, medical/dental group, med spa, fertility center, urgent care, or physician-owned specialty group operating under its OWN brand within a single metro or region. A handful of locations (roughly up to ~8 sites) is still independent. This is the DEFAULT when there is no clear franchise / private-equity / national-chain signal.
- telehealth_platform: A national virtual-care, behavioral-health, or telehealth network/marketplace that signs up and contracts clinicians at scale (e.g. Headway, Talkiatry, Thriveworks, SonderMind, Grow Therapy, Charlie Health, Brightside, Two Chairs, Cerebral). Continuous nationwide clinician recruiting is core to its business.
- hospital_system: A hospital, health system, academic medical center, regional medical center, or large integrated delivery network with its own internal recruiting team (e.g. Tenet, CommonSpirit, UNC Health, Northwestern Medicine, Baptist Health System, Acadia).
- chain_franchise: ONLY clear cases — a FRANCHISE (individually-owned franchised locations, e.g. American Family Care, Milan Laser, FYZICAL, dermani MEDSPA, THE DRIPBaR), a PRIVATE-EQUITY / MSO ROLL-UP or management-services platform (e.g. Forefront Dermatology, American Oncology Network, Unio Health Partners, Physician Partners of America), or a LARGE NATIONAL / MULTI-STATE chain with many sites and centralized hiring (e.g. Peachtree Immediate Care, JumpstartMD, Medi-Weightloss).

Decision rule: choose chain_franchise ONLY when the name/description/website clearly signals a franchise, PE/MSO backing, or a large national multi-state chain (10+ sites or "nationwide"). If it is just a locally/regionally branded group with no such signal, choose independent_practice — even with a few locations. When genuinely unsure between independent_practice and chain_franchise, choose independent_practice.

Use company name, description, website, and the sample job description.

Return ONLY valid JSON: {"category": "independent_practice|telehealth_platform|hospital_system|chain_franchise", "reason": "<one short sentence>"}"""

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


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_gid_from_url(url):
    m = re.search(r"[#&?]gid=(\d+)", url)
    return int(m.group(1)) if m else None


def resolve_tab(service, sheet_id, url):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"]
    return meta["sheets"][0]["properties"]["title"]


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
            return {"category": "independent_practice", "reason": "no JSON (defaulted)"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"category": "independent_practice", "reason": f"error: {e}"}


def main():
    parser = argparse.ArgumentParser(description="Classify employer type (ICP vs platform/system/chain)")
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=LLM_WORKERS)
    parser.add_argument("--write_labels", action="store_true", help="Write Employer Type/Reason to cols X/Y")
    parser.add_argument("--reclassify", action="store_true", help="Re-classify even already-labeled companies")
    args = parser.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    tab = resolve_tab(service, sheet_id, args.sheet_url)

    mode = "WRITE LABELS — classify + write cols X/Y" if args.write_labels else "READ-ONLY report"
    print(f"=== Classify Employer Type ({mode}) ===")
    print(f"Tab:   {tab!r}")
    print(f"Model: {AZURE_DEPLOYMENT}\n")

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A2:Y10000"
    ).execute().get("values", [])
    print(f"Total rows: {len(rows)}")

    groups = OrderedDict()
    for i, r in enumerate(rows):
        sheet_row = i + 2
        name = safe_get(r, COL_COMPANY_NAME)
        if not name:
            continue
        key = normalize_company_url(safe_get(r, COL_ABOUT_LINK)) or name.lower()
        if key not in groups:
            groups[key] = {
                "name": name, "website": safe_get(r, COL_WEBSITE),
                "company_desc": safe_get(r, COL_COMPANY_DESC),
                "job_title": safe_get(r, COL_TITLE), "job_desc": safe_get(r, COL_JOB_DESC),
                "existing_label": safe_get(r, COL_LABEL), "rows": 0, "row_nums": [],
            }
        groups[key]["rows"] += 1
        groups[key]["row_nums"].append(sheet_row)
        if not groups[key]["company_desc"]:
            groups[key]["company_desc"] = safe_get(r, COL_COMPANY_DESC)

    keys = list(groups.keys())
    if args.limit:
        keys = keys[:args.limit]

    to_classify = keys
    if args.write_labels and not args.reclassify:
        to_classify = [k for k in keys if not groups[k]["existing_label"]]
        print(f"Unique companies: {len(keys)}  (already labeled: {len(keys) - len(to_classify)}, to classify: {len(to_classify)})\n")
    else:
        print(f"Unique companies: {len(keys)}\n")

    results = {}
    for k in keys:
        if groups[k]["existing_label"]:
            results[k] = {"category": groups[k]["existing_label"], "reason": "(existing label)"}

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

    # Tally (companies + rows per category)
    cat_co = {c: 0 for c in CATEGORIES}
    cat_rows = {c: 0 for c in CATEGORIES}
    by_cat = {c: [] for c in CATEGORIES}
    for key in keys:
        c = results.get(key, {}).get("category", "independent_practice")
        if c not in cat_co:
            c = "independent_practice"
        cat_co[c] += 1
        cat_rows[c] += groups[key]["rows"]
        by_cat[c].append((groups[key]["name"], groups[key]["rows"]))

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"{'category':22s} {'companies':>10s} {'job rows':>10s}")
    for c in CATEGORIES:
        print(f"{c:22s} {cat_co[c]:>10d} {cat_rows[c]:>10d}")

    for c in ("telehealth_platform", "hospital_system", "chain_franchise"):
        print(f"\n{c} ({cat_co[c]} companies):")
        for name, n in sorted(by_cat[c], key=lambda x: -x[1]):
            print(f"  [{n:2d}x] {name[:46]}")

    if not args.write_labels:
        print("\n[READ-ONLY] No changes. Run --write_labels to freeze these into cols X/Y for review.")
        return

    updates = [
        {"range": f"'{tab}'!{col_letter(COL_LABEL)}1", "values": [[LABEL_HEADER]]},
        {"range": f"'{tab}'!{col_letter(COL_LABEL_REASON)}1", "values": [[REASON_HEADER]]},
    ]
    for key in to_classify:
        res = results.get(key, {})
        label = res.get("category", "independent_practice")
        reason = res.get("reason", "")[:300]
        for rn in groups[key]["row_nums"]:
            updates.append({"range": f"'{tab}'!{col_letter(COL_LABEL)}{rn}", "values": [[label]]})
            updates.append({"range": f"'{tab}'!{col_letter(COL_LABEL_REASON)}{rn}", "values": [[reason]]})

    for i in range(0, len(updates), 500):
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": updates[i:i + 500]}
        ).execute()
    print(f"\nWrote Employer Type for {len(to_classify)} companies to cols "
          f"{col_letter(COL_LABEL)}/{col_letter(COL_LABEL_REASON)}. Review the borderline ones in the sheet.")


if __name__ == "__main__":
    main()
