"""
Build two tabs from the source LinkedIn-jobs sheet, filtered to independent_practice
(col X label written by classify_employer_type.py):

  "Multiple Openings" — companies with >1 posting. One row per opening, grouped by
                        company (most openings first), with two extra columns:
                          Openings        = count for that company
                          Openings Detail = roll-up of "Title — Location — Posted date"
  "Single Opening"    — companies with exactly 1 posting. One row each.

Carries A-W (job + company info incl. website/size/description). Drops the X/Y label
columns. Source tab is NOT modified. Re-runnable: clears the target tabs if present.

Usage:
  python3 -W ignore build_opening_tabs.py --sheet_url "URL" [--label_col_filter independent_practice]
"""

import re
import argparse
from collections import OrderedDict

from pull_dataset import get_google_service, get_sheet_id_from_url

COL_TITLE = 0          # A
COL_LOCATION = 7       # H
COL_CREATED_AT = 14    # O
COL_COMPANY_NAME = 12  # M
COL_ABOUT_LINK = 19    # T
COL_LABEL = 23         # X — Employer Type
LAST_CARRY_COL = 22    # carry A..W (0..22)

MULTI_TAB = "Multiple Openings"
SINGLE_TAB = "Single Opening"


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
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    gid = get_gid_from_url(url)
    for s in meta["sheets"]:
        if gid is not None and s["properties"]["sheetId"] == gid:
            return s["properties"]["title"]
    return meta["sheets"][0]["properties"]["title"]


def existing_tabs(service, sheet_id):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def ensure_blank_tab(service, sheet_id, title):
    """Create the tab if missing, else clear its contents. Returns nothing."""
    tabs = existing_tabs(service, sheet_id)
    if title in tabs:
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{title}'"
        ).execute()
    else:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()


def set_row_height(service, sheet_id, title, pixels=18):
    """Pin every row in the tab to a fixed pixel height (newlines otherwise blow rows up)."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            gid = s["properties"]["sheetId"]
            rc = s["properties"]["gridProperties"]["rowCount"]
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"updateDimensionProperties": {
                    "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": rc},
                    "properties": {"pixelSize": pixels}, "fields": "pixelSize"}}]},
            ).execute()
            return


def opening_line(row):
    title = safe_get(row, COL_TITLE) or "(no title)"
    loc = safe_get(row, COL_LOCATION) or "(no location)"
    posted = safe_get(row, COL_CREATED_AT)[:10] or "(no date)"
    return f"{title} — {loc} — {posted}"


def write_tab(service, sheet_id, title, header, data_rows):
    ensure_blank_tab(service, sheet_id, title)
    values = [header] + data_rows
    # write in chunks of 500 rows
    start = 1
    for i in range(0, len(values), 500):
        chunk = values[i:i + 500]
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{title}'!A{start}",
            valueInputOption="RAW",
            body={"values": chunk},
        ).execute()
        start += len(chunk)


def main():
    ap = argparse.ArgumentParser(description="Build Single/Multiple opening tabs from independent practices")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--label_col_filter", default="independent_practice",
                    help="Employer Type (col X) value to keep (default independent_practice)")
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    src_tab = resolve_tab(service, sheet_id, args.sheet_url)

    print("=== Build Opening Tabs ===")
    print(f"Source tab: {src_tab!r}")
    print(f"Keep label: {args.label_col_filter!r}\n")

    header_resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{src_tab}'!A1:W1"
    ).execute().get("values", [[]])
    src_header = header_resp[0] if header_resp else []
    # pad header to 23 cols
    src_header = (src_header + [""] * (LAST_CARRY_COL + 1))[: LAST_CARRY_COL + 1]
    out_header = src_header + ["Openings", "Openings Detail"]

    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{src_tab}'!A2:Y10000"
    ).execute().get("values", [])
    print(f"Source rows: {len(rows)}")

    # Group independent_practice rows by company key
    groups = OrderedDict()
    kept = 0
    for r in rows:
        name = safe_get(r, COL_COMPANY_NAME)
        if not name:
            continue
        if safe_get(r, COL_LABEL).lower() != args.label_col_filter.lower():
            continue
        kept += 1
        key = normalize_company_url(safe_get(r, COL_ABOUT_LINK)) or name.lower()
        groups.setdefault(key, {"name": name, "rows": []})
        groups[key]["rows"].append(r)

    print(f"Kept ({args.label_col_filter}): {kept} rows across {len(groups)} companies\n")

    singles = {k: v for k, v in groups.items() if len(v["rows"]) == 1}
    multis = {k: v for k, v in groups.items() if len(v["rows"]) > 1}

    def carry(row):
        return (row + [""] * (LAST_CARRY_COL + 1))[: LAST_CARRY_COL + 1]

    # ---- Multiple Openings: most openings first; within company, newest first ----
    multi_out = []
    for key in sorted(multis, key=lambda k: -len(multis[k]["rows"])):
        company_rows = sorted(multis[key]["rows"],
                              key=lambda r: safe_get(r, COL_CREATED_AT)[:10], reverse=True)
        count = len(company_rows)
        detail = "\n".join(opening_line(r) for r in company_rows)
        for r in company_rows:
            multi_out.append(carry(r) + [count, detail])

    # ---- Single Opening: one row each, by company name ----
    single_out = []
    for key in sorted(singles, key=lambda k: singles[k]["name"].lower()):
        r = singles[key]["rows"][0]
        single_out.append(carry(r) + [1, ""])

    write_tab(service, sheet_id, MULTI_TAB, out_header, multi_out)
    write_tab(service, sheet_id, SINGLE_TAB, out_header, single_out)
    set_row_height(service, sheet_id, MULTI_TAB, 18)
    set_row_height(service, sheet_id, SINGLE_TAB, 18)

    print(f"Wrote {MULTI_TAB!r}:  {len(multis)} companies / {len(multi_out)} rows")
    print(f"Wrote {SINGLE_TAB!r}: {len(singles)} companies / {len(single_out)} rows")
    print(f"\nDone. Source tab {src_tab!r} untouched.")


if __name__ == "__main__":
    main()
