"""
Phase 2 (icebreaker research) - assemble one research pack per lead, ready
for Claude (me) to read directly and write the icebreaker myself.

NO LLM call anywhere in this script (Jude, 2026-08-12: "the entire pipeline,
no LLM anywhere"). Unlike personalized-icebreakers/build_dossier.py, there is
no GPT-4.1 synthesis step here — this script only assembles raw sources
(DM LinkedIn profile JSON from scrape_dm_linkedin.py + site pages JSON from
scrape_company_facts.py + sheet identity columns) into one JSON file. Fact
extraction AND icebreaker writing both happen in the next step, by me,
reading this file — not by a model call.

Writes --out FILE as a JSON list of research packs:
  [{"row", "first", "last", "title", "company", "casual_company",
    "website", "linkedin": {...compact profile...},
    "site_pages": [{"url","text"}, ...]}, ...]

Row is eligible once it has DM LinkedIn data AND site pages AND no icebreaker
yet (--col_icebreaker empty). Resume-safe by construction: rerunning after
apply_icebreakers.py has written lines automatically shrinks the pending set.

Run:
  python3 -W ignore collect_icebreaker_research.py --sheet_url "URL" \
    --tab Leads --out data/icebreaker_batchN.json [--limit N]
"""
import os
import re
import json
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

COL_COMPANY = 10       # K
COL_WEBSITE = 11       # L
COL_DM_NAME = 19       # T
COL_DM_TITLE = 20      # U
COL_DM_LINKEDIN_URL = 21  # V
COL_FIRST = 23         # X
COL_LAST = 24          # Y
COL_DM_LINKEDIN_DATA = 29  # AD (scrape_dm_linkedin.py)
COL_SITE_PAGES = 30    # AE (scrape_company_facts.py)
COL_ICEBREAKER = 31    # AF (apply_icebreakers.py writes here)


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


def casual_company(name):
    """Same rule as generate_icebreaker.py: strip legal suffixes / generic
    tails, lowercase (people typing fast don't shift-key brand names)."""
    n = re.sub(r"[,.]", " ", name or "").strip()
    n = re.sub(r"\b(LLC|L\.L\.C|Inc|Incorporated|Ltd|Corp|Corporation|Co|Group|"
               r"Productions|Production|Films|Film|Media|Studios|Studio|"
               r"Pictures|Creative|Entertainment|Content|Video|Visuals|"
               r"Partners|Associates|International)\b.*$", "", n, flags=re.I).strip(" ,-&")
    out = n if len(n) >= 3 else (name or "").strip()
    return out.lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()

    last_col = col_letter(COL_ICEBREAKER)
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last_col}").execute().get("values", [])[1:]

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    packs = []
    for i, r in enumerate(rows):
        li_raw = cell(r, COL_DM_LINKEDIN_DATA)
        site_raw = cell(r, COL_SITE_PAGES)
        existing_ice = cell(r, COL_ICEBREAKER)
        if not li_raw or not site_raw or existing_ice:
            continue
        try:
            li = json.loads(li_raw)
        except Exception:
            li = {}
        try:
            site = json.loads(site_raw)
        except Exception:
            site = {}
        company = cell(r, COL_COMPANY)
        packs.append({
            "row": i + 2,
            "first": cell(r, COL_FIRST) or li.get("name", "").split(" ")[0],
            "last": cell(r, COL_LAST),
            "title": cell(r, COL_DM_TITLE) or li.get("title", ""),
            "company": company,
            "casual_company": casual_company(company),
            "website": cell(r, COL_WEBSITE),
            "linkedin": li,
            "site_pages": (site or {}).get("pages", []),
        })

    if args.limit:
        packs = packs[:args.limit]

    with open(args.out, "w") as f:
        json.dump(packs, f, ensure_ascii=False, indent=1)

    print(f"=== Collect Icebreaker Research ===")
    print(f"Eligible rows (DM LinkedIn + site pages, no icebreaker yet): {len(packs)}")
    print(f"-> {args.out}")
    print(f"\nNext: read this file directly (or fan out subagents) and write each "
          f"lead's icebreaker line following the 'Love X. Also Y.' v3 formula. "
          f"Save verdicts as a JSON list of {{'row','icebreaker','fact_type'}} "
          f"(fact_type in founder_background/published_numbers/awards/milestone/"
          f"credentials/niche/none), then apply with apply_icebreakers.py.")


if __name__ == "__main__":
    main()
