"""
Assign the outreach Target Role to each ICP company on the Multiple/Single tabs.

Jude's routing rule:
  - single location  (all openings in ONE local metro)   -> CEO/Owner
  - multiple locations (openings span MULTIPLE metros)    -> COO
  Never target the Practice Manager (no budget authority).

Single Opening tab: every company is one opening -> single metro -> CEO/Owner.
Multiple Openings tab: a company can be single-metro (e.g. Cypress/Houston/Katy =
  Houston metro) or multi-metro (San Diego, CA + Tyler, TX). GPT-4.1 judges the
  metro spread from the distinct opening locations (it knows US geography, which
  a naive distinct-city count does not).

Writes a "Target Role" column (Z) on both tabs. Re-runnable.

Usage:
  python3 -W ignore assign_target_role.py --sheet_url "URL" [--workers 8]
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

MULTI_TAB = "Multiple Openings"
SINGLE_TAB = "Single Opening"

COL_COMPANY_NAME = 12  # M
COL_LOCATION = 7       # H
COL_TARGET = 25        # Z
TARGET_HEADER = "Target Role"

TARGET_SINGLE = "CEO/Owner"
TARGET_MULTI = "COO"

SPREAD_SYSTEM = """You are given a healthcare company and the DISTINCT locations of its current job openings. Decide whether the openings are all within ONE local metro area (one city plus its suburbs/exurbs forming a single job market) or span MULTIPLE distinct metro areas.

Examples:
- Cypress, Houston, Katy => single_metro (all Houston metro)
- The Woodlands, Tomball => single_metro (north Houston metro)
- Helotes, New Braunfels, San Antonio => single_metro (San Antonio area)
- San Diego, CA + Tyler, TX => multi_metro
- Napa + Sacramento => multi_metro (different markets)
- Avondale, Tucson, Yuma (AZ) => multi_metro

If the locations are too vague to tell (only "United States", or just a state with no city), answer single_metro.

Return ONLY JSON: {"spread": "single_metro|multi_metro", "reason": "<short>"}"""


def safe_get(row, idx):
    return row[idx].strip() if len(row) > idx and row[idx] else ""


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


def tab_exists(service, sheet_id, title):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def classify_spread(client, company, locations):
    loc_list = "\n".join(f"- {l}" for l in locations) or "- (none)"
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=120, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SPREAD_SYSTEM},
                {"role": "user", "content": f"Company: {company}\nDistinct opening locations:\n{loc_list}\n\nReturn JSON."},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else {"spread": "single_metro", "reason": "no JSON"}
    except Exception as e:
        return {"spread": "single_metro", "reason": f"error: {e}"}


def write_target_col(service, sheet_id, tab, row_to_target):
    updates = [{"range": f"'{tab}'!{col_letter(COL_TARGET)}1", "values": [[TARGET_HEADER]]}]
    for rn, target in row_to_target.items():
        updates.append({"range": f"'{tab}'!{col_letter(COL_TARGET)}{rn}", "values": [[target]]})
    for i in range(0, len(updates), 500):
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": updates[i:i + 500]}
        ).execute()


def main():
    ap = argparse.ArgumentParser(description="Assign Target Role (CEO/Owner vs COO) to opening tabs")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)

    print("=== Assign Target Role ===")
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)

    # ---- Single Opening tab: all CEO/Owner ----
    if tab_exists(service, sheet_id, SINGLE_TAB):
        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{SINGLE_TAB}'!A2:Z10000"
        ).execute().get("values", [])
        row_to_target = {i + 2: TARGET_SINGLE for i, r in enumerate(rows) if safe_get(r, COL_COMPANY_NAME)}
        write_target_col(service, sheet_id, SINGLE_TAB, row_to_target)
        print(f"{SINGLE_TAB}: {len(row_to_target)} rows -> {TARGET_SINGLE}")

    # ---- Multiple Openings tab: classify metro spread per company ----
    if not tab_exists(service, sheet_id, MULTI_TAB):
        print(f"[!] {MULTI_TAB!r} not found.")
        return

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{MULTI_TAB}'!A2:Z10000"
    ).execute().get("values", [])

    companies = OrderedDict()  # name -> {"locs": set, "row_nums": []}
    for i, r in enumerate(rows):
        name = safe_get(r, COL_COMPANY_NAME)
        if not name:
            continue
        companies.setdefault(name, {"locs": set(), "row_nums": []})
        loc = safe_get(r, COL_LOCATION)
        if loc:
            companies[name]["locs"].add(loc)
        companies[name]["row_nums"].append(i + 2)

    print(f"{MULTI_TAB}: {len(companies)} companies, classifying metro spread with {AZURE_DEPLOYMENT}...\n")

    def run(name):
        return name, classify_spread(client, name, sorted(companies[name]["locs"]))

    spread = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run, n): n for n in companies}
        for fut in as_completed(futs):
            name, res = fut.result()
            spread[name] = res

    row_to_target = {}
    n_single = n_multi = 0
    for name, d in companies.items():
        sp = spread[name].get("spread", "single_metro")
        target = TARGET_MULTI if sp == "multi_metro" else TARGET_SINGLE
        if target == TARGET_MULTI:
            n_multi += 1
        else:
            n_single += 1
        for rn in d["row_nums"]:
            row_to_target[rn] = target
        print(f"  {target:10s}  [{len(d['locs'])} loc] {name[:44]:44s} — {spread[name].get('reason','')[:50]}")

    write_target_col(service, sheet_id, MULTI_TAB, row_to_target)
    print(f"\n{MULTI_TAB}: {n_single} companies -> {TARGET_SINGLE}, {n_multi} companies -> {TARGET_MULTI}")
    print(f"Wrote Target Role to col {col_letter(COL_TARGET)} on both tabs.")


if __name__ == "__main__":
    main()
