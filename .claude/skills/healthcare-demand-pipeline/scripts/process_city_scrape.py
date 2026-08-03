"""
Phase 1.5 — process a raw city-grid scrape sheet into the main campaign sheet.

ORDER IS A HARD RULE (Jude): the freshness window comes FIRST, before any
dedupe or enrichment. Nothing older than the window ever lands on the main
sheet.

WINDOW IS 35 DAYS as of 2026-07-31 (was 60). Measured on the Indiana + Texas
campaigns: postings <=30 days old at first contact replied at 2.35%, 31-60
days at 0.88%, and >60 days at 0.00% (0 replies from 46 sends). The 46-60 day
slice was 148 leads that returned one reply. 35 rather than 30 because the
sequence runs about a week, so a lead entering at 35 days is still inside the
productive band at its last touch.

Do NOT narrow this to a 25-35 "sweet spot" band — the fresh end carries the
result. Postings 0-25 days old replied at 2.42%; a 25-35 band alone replies
at 1.23%, worse than keeping everything under 35.

Steps:
  1. Freshness window: keep postings with Date Published >= today - cutoff_days.
  2. Dedupe by company: winner = OLDEST posting INSIDE the window
     (longest-open-but-still-live = max pain).
  3. Agency/university name filter (staffing, locum, recruiting, university...).
  4. Conservative LLM classify (GPT-4.1): drop ONLY clear-cut staffing agencies,
     job boards, government bodies. Schools and hospital chains are KEPT
     (client rule for healthcare demand; use --drop_schools to exclude them).
     National retail corps get appended but flagged skip_national_corp.
  5. Append ONLY companies not already on the main sheet (29-col schema A-S,
     status col AB set for corp flags).

SIZE CAP: 500 employees (Jude, 2026-07-31), applied right after the freshness
window. Reply rate by band on Indiana + Texas: TINY <50 3.52%, size-unknown
2.74%, MID 50-499 1.20%, LARGE 500+ 0.00% (0 replies from 251 companies).
Blank/unknown size is KEPT — those rows reply at 2.74% and are mostly small
independents. Accepted cost: Indeed reports the employer BRAND's headcount, so
chain facilities inherit the parent's size and the cap removes them too (nine
Life Care Center homes tagged 40,000). The run prints a sample of what it
dropped. --max_employees 0 disables.

Run:
  python3 -W ignore process_city_scrape.py \
    --raw_sheet_url "RAW_URL" --main_sheet_url "MAIN_URL" \
    [--cutoff_days 35] [--max_employees 500] [--drop_schools] [--dry_run]
"""

import os
import re
import json
import argparse
from datetime import date, timedelta

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import AzureOpenAI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

TAB = "Leads"
CLASSIFY_CHUNK = 40
# "college" added 2026-08-01: Jude's rule is no universities, and the LLM
# classifier labels colleges "school", which the client rule KEEPS (K-12
# districts are valid targets — they hire SLPs and OTs). Tertiary education is
# not. Checked against the Florida sheet: matches Southeastern College and HCI
# College only; "South Campus Care Center" is unaffected because it has no
# "college" token.
AGENCY_UNI = ["university", "college", "staffing", "locum", "recruiting",
              "recruitment", "travel nurs"]

SYSTEM = """Classify companies that posted healthcare jobs. Return JSON:
{"verdicts": [{"i": <index>, "label": "<label>"}]}
Labels:
- "keep"          — default. Private practices, clinics, imaging centers, dental
                    offices, pharmacies, labs, therapy clinics, senior care,
                    hospitals and hospital systems, home health, EMS, schools.
- "agency"        — ONLY if clearly a staffing/recruitment/travel-clinician firm.
- "job_board"     — ONLY if clearly a job board or aggregator.
- "government"    — ONLY if clearly a government body (state, county, VA, prison).
- "school"        — schools/districts/colleges (kept unless --drop_schools).
- "national_corp" — ONLY household-name national retail/corporate chains
                    (e.g. Walmart, CVS, Walgreens, Kroger, Meijer, Amazon).
If uncertain in ANY way, use "keep". Every index must get a verdict."""


def sheet_id_of(url):
    return re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url).group(1)


def is_agency_uni(name):
    n = (name or "").lower()
    return any(a in n for a in AGENCY_UNI)


def norm_company(name):
    n = (name or "").lower()
    n = re.sub(r"\b(inc|llc|pllc|pc|llp|ltd|corp|corporation|co|company|group|"
               r"medical|health|healthcare|clinic|practice|center|centre|the)\b\.?", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def parse_size_lower_bound(size_str):
    """Indeed returns ranges like '11 to 50', '201 to 500', '10,000+'.
    Returns the LOWER bound as int, or None if unparseable/blank.
    Lower bound is deliberate: a '201 to 500' org is under a 500 cap, and one
    of the three interested replies so far was exactly that band."""
    if not size_str:
        return None
    s = str(size_str).strip().replace(",", "").replace("+", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    if not s:
        return None
    try:
        return int(s.split("-")[0])
    except (ValueError, IndexError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw_sheet_url", required=True)
    ap.add_argument("--main_sheet_url", required=True)
    ap.add_argument("--cutoff_days", type=int, default=35)
    ap.add_argument("--max_employees", type=int, default=500,
                    help="Drop employers above this headcount (default 500). "
                         "0 disables the cap. Blank/unknown size is always "
                         "kept — those rows reply at 2.74%.")
    ap.add_argument("--drop_schools", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    cutoff = (date.today() - timedelta(days=args.cutoff_days)).isoformat()
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    svc = build("sheets", "v4", credentials=creds)

    raw = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id_of(args.raw_sheet_url),
        range=f"{TAB}!A:S").execute().get("values", [])[1:]
    print(f"raw postings: {len(raw)}")

    # 1. freshness window FIRST (hard rule)
    inwin = [r for r in raw if len(r) > 4 and r[4] >= cutoff]
    print(f"within {args.cutoff_days}-day window (>= {cutoff}): {len(inwin)}")

    # 1b. company-size cap (Jude, 2026-07-31). Reply rate by band on Indiana +
    # Texas: TINY <50 3.52%, size-unknown 2.74%, MID 50-499 1.20%, LARGE 500+
    # 0.00% (0 replies from 251 companies). Blank/unparseable size is KEPT —
    # unknown-size rows reply at 2.74% and are mostly small independents.
    # Col M (index 12) is Company Size.
    if args.max_employees:
        def _too_big(r):
            lb = parse_size_lower_bound(r[12] if len(r) > 12 else "")
            return lb is not None and lb > args.max_employees
        before = len(inwin)
        dropped = [r for r in inwin if _too_big(r)]
        inwin = [r for r in inwin if not _too_big(r)]
        print(f"after {args.max_employees}-employee cap: {len(inwin)} "
              f"(dropped {before - len(inwin)})")
        # Chain facilities inherit the PARENT's headcount from Indeed, so the
        # cap removes them too. Surface it rather than dropping them silently.
        if dropped:
            names = ", ".join(sorted({(r[10] if len(r) > 10 else "?") for r in dropped})[:6])
            print(f"  note: cap also removes chain facilities that inherited a "
                  f"parent's size. Sample dropped: {names}")

    # 2. dedupe by company — oldest in-window posting wins
    best = {}
    for r in inwin:
        key = norm_company(r[10] if len(r) > 10 else "")
        if not key:
            continue
        if key not in best or r[4] < best[key][4]:
            best[key] = r
    print(f"unique companies: {len(best)}")

    # 3. agency/university name filter
    best = {k: r for k, r in best.items() if not is_agency_uni(r[10])}
    print(f"after name filter: {len(best)}")

    # 4. drop companies already on main sheet
    main_id = sheet_id_of(args.main_sheet_url)
    existing_rows = svc.spreadsheets().values().get(
        spreadsheetId=main_id, range=f"{TAB}!K:K").execute().get("values", [])
    existing = {norm_company(r[0]) for r in existing_rows[1:] if r}
    new = {k: r for k, r in best.items() if k not in existing}
    print(f"new companies vs main sheet: {len(new)}")

    # 5. conservative LLM classify
    client = AzureOpenAI(azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                         api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                         api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))
    items = list(new.items())
    drops, corps, schools = set(), set(), set()
    for start in range(0, len(items), CLASSIFY_CHUNK):
        chunk = items[start:start + CLASSIFY_CHUNK]
        listing = "\n".join(
            f"{i}: {r[10]} | posting: {r[1] if len(r) > 1 else ''} | "
            f"{(r[17] + ', ' + r[18]) if len(r) > 18 else ''}"
            for i, (k, r) in enumerate(chunk))
        try:
            resp = client.chat.completions.create(
                model=os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1"),
                max_tokens=1500, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": listing}])
            verdicts = json.loads(resp.choices[0].message.content).get("verdicts", [])
        except Exception as e:
            print(f"  [!] classify chunk {start}: {type(e).__name__} — keeping all")
            verdicts = []
        for v in verdicts:
            i = v.get("i")
            if not isinstance(i, int) or not (0 <= i < len(chunk)):
                continue
            key, label = chunk[i][0], v.get("label", "keep")
            if label in ("agency", "job_board", "government"):
                drops.add(key)
            elif label == "school":
                (drops if args.drop_schools else schools).add(key)
            elif label == "national_corp":
                corps.add(key)
        print(f"  classified {min(start + CLASSIFY_CHUNK, len(items))}/{len(items)}")

    keepers = [(k, r) for k, r in items if k not in drops]
    print(f"LLM dropped: {len(drops)} | national_corp flagged: {len(corps)} | "
          f"schools kept: {len(schools)}")
    print(f"appending: {len(keepers)}")
    if args.dry_run or not keepers:
        return

    keepers.sort(key=lambda kr: kr[1][4])
    next_row = len(existing_rows) + 1
    rows_out = []
    for k, r in keepers:
        row = (r + [""] * 19)[:19] + [""] * 8
        row.append("skip_national_corp" if k in corps else "")  # AB
        job_id = (r[0] or "").strip() if r else ""
        row.append(f"https://www.indeed.com/viewjob?jk={job_id}" if job_id else "")  # AC — Indeed URL always lives in AC (Jude's rule)
        rows_out.append(row)
    for i in range(0, len(rows_out), 1000):
        svc.spreadsheets().values().update(
            spreadsheetId=main_id, range=f"{TAB}!A{next_row + i}",
            valueInputOption="RAW", body={"values": rows_out[i:i + 1000]}).execute()

    meta = svc.spreadsheets().get(spreadsheetId=main_id).execute()
    tab_id = next(s["properties"]["sheetId"] for s in meta["sheets"]
                  if s["properties"]["title"] == TAB)
    svc.spreadsheets().batchUpdate(spreadsheetId=main_id, body={"requests": [{
        "updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "ROWS",
                      "startIndex": next_row - 1,
                      "endIndex": next_row - 1 + len(rows_out)},
            "properties": {"pixelSize": 18}, "fields": "pixelSize"}}]}).execute()
    print(f"appended {len(rows_out)} rows at row {next_row}")


if __name__ == "__main__":
    main()
