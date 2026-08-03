"""Find a decision maker's NAME via Exa, for companies Apollo and AMF cannot reach.

The gap this fills: on the Florida sheet 349 companies had nobody in Apollo's
index AND nothing from AnyMail Finder's /decision-maker on either `ceo` or
`operations`. Those are dead ends for both tools, but Exa's index does carry
them. A measured 8-company probe found the owner or CEO on LinkedIn for 2 of 8
even with a crude regex, plus a third whose founder name sat in the result URL.

This writes a NAME ONLY. It never invents an email. Feed the result to
amf_person_fill.py, which resolves the email at 1 credit per found.

Extraction is done by GPT-4.1 rather than a regex, because the regex version
returned "View Website", "Main Content" and "Board Certified" as people. The
model gets the search results and must return a real human name and title, or
nothing.

GUARDS (a wrong name is worse than a blank, it burns the company):
  * The person must be tied to THIS company in the source text, not merely
    appear near it. Directory pages list many businesses on one page.
  * Ladder order is Jude's: CEO/Owner/Founder/President -> COO -> company-level
    Medical Director. HR, clinical staff and practice managers are excluded.
  * Directory and aggregator sources are dropped before the model sees them.
  * Two-part human names only. Single tokens and title-case noise are rejected.

  python3 -W ignore find_dm_exa.py --sheet_url "URL" --limit 20          # dry run
  python3 -W ignore find_dm_exa.py --sheet_url "URL" --limit 20 --apply
"""
import os
import re
import json
import argparse
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

EXA_URL = "https://api.exa.ai/search"
EXA_COST = 0.007
AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

# Pages that list many businesses at once. A name near the company on one of
# these is not evidence the person works there.
JUNK_SRC = ("allpages.com", "yellowpages", "manta.com", "bizapedia", "dnb.com",
            "buzzfile", "corporationwiki", "opengovus", "bbb.org", "yelp.com",
            "indeed.com", "ziprecruiter", "glassdoor", "zoominfo", "apollo.io",
            "healthgrades", "zocdoc", "vitals.com", "uli.org", "facebook.com",
            "crunchbase", "opencorporates", "sunbiz.org", "npidb", "npino")

SYSTEM = """You identify ONE decision maker at a specific healthcare company from
search results. Reply with ONLY JSON, no prose.

{"name": "First Last", "title": "...", "source": "<url that proves it>"}

Return {"name": "", "title": "", "source": ""} when you cannot prove it.

LADDER, in order. Take the first you can actually evidence:
  1. CEO / Owner / Co-Owner / Founder / Co-Founder / President / Managing Partner
  2. COO / Chief Operating Officer
  3. Medical Director, but ONLY a company-level one. At a small clinic that
     title is usually a contracted outside physician with no hiring authority.

NEVER return: HR of any level, practice or office managers, clinical staff
(nurses, therapists, technologists, medical assistants), or anyone whose
source shows they work for a DIFFERENT company.

HARD RULES
- The source text must tie the person to THIS company by name. Appearing on the
  same page is not enough; directory pages list dozens of businesses.
- "name" must be a real human name, two parts minimum. Never a fragment like
  "View Website", "Main Content", "Board Certified", "Our Team".
- If several people qualify, take the highest rung, then the one with the
  clearest evidence.
- When in doubt, return empty. A wrong name burns the company permanently."""

# Allows a middle initial ("Jonathan M. Frantz") — an earlier version rejected
# that as malformed, discarding a real person on a formatting technicality.
L = "A-Za-zÀ-ÖØ-öø-ÿ"
NAME_OK = re.compile(
    rf"^[{L}][{L}'\-]{{1,20}}(?: (?:[{L}]\.?|[{L}][{L}'\-]{{1,20}})){{1,3}}$")
BAD_NAME = re.compile(r"view|website|main content|board certified|our team|"
                      r"contact|home|about|learn more|read more|click", re.I)

HONORIFIC = re.compile(r"^\s*(dr|mr|mrs|ms|miss|prof)\.?\s+", re.I)
SUFFIX = re.compile(r"[,\s]+(m\.?d\.?|d\.?o\.?|ph\.?d\.?|psy\.?d\.?|d\.?p\.?t\.?|"
                    r"r\.?n\.?|n\.?p\.?|p\.?a\.?|dds|dmd|mba|fache|jr|sr|ii|iii|iv)\.?\s*$",
                    re.I)


def clean_person_name(n):
    """Strip honorifics, credentials and generational suffixes.

    Real people were being discarded on formatting alone: "Dr. Akler",
    "Chintan Desai M.D.", "Robert Hruby, MD", "Michael Crimi Jr.". Normalising
    is the fix; widening the validator would let junk back in.
    """
    n = " ".join((n or "").split())
    n = HONORIFIC.sub("", n)
    prev = None
    while prev != n:                      # "Name, MD, FACHE" needs two passes
        prev = n
        n = SUFFIX.sub("", n).strip(" ,.")
    return n


def exa(company, city, key):
    q = f"{company} {city} Florida owner OR CEO OR founder OR president"
    try:
        r = requests.post(EXA_URL,
                          headers={"x-api-key": key, "Content-Type": "application/json"},
                          json={"query": q, "numResults": 6, "type": "auto",
                                "contents": {"text": {"maxCharacters": 900}}},
                          timeout=45)
    except requests.RequestException as e:
        return [], 0.0, f"error:{type(e).__name__}"
    if r.status_code == 402:
        raise SystemExit("Exa out of credits (402). Top up at dashboard.exa.ai")
    if r.status_code != 200:
        return [], 0.0, f"error:http_{r.status_code}"
    d = r.json()
    res = [x for x in d.get("results", [])
           if not any(j in (x.get("url") or "").lower() for j in JUNK_SRC)]
    return res, (d.get("costDollars") or {}).get("total", 0.0) or 0.0, None


def extract(llm, company, city, results):
    if not results:
        return None, None, "no_results"
    blob = "\n\n".join(
        f"URL: {x.get('url','')}\nTITLE: {x.get('title','')}\nTEXT: {(x.get('text') or '')[:700]}"
        for x in results[:5])
    try:
        resp = llm.chat.completions.create(
            model=AZURE_DEPLOYMENT, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user",
                       "content": f"COMPANY: {company}\nCITY: {city}, FL\n\n{blob}"}])
        d = json.loads(resp.choices[0].message.content)
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"
    name = clean_person_name(d.get("name"))
    title = " ".join((d.get("title") or "").split())
    if not name:
        return None, None, "no_person"
    if BAD_NAME.search(name) or not NAME_OK.match(name):
        return None, None, f"rejected_name:{name[:28]}"
    return name, title or "CEO", None


ap = argparse.ArgumentParser()
ap.add_argument("--sheet_url", required=True)
ap.add_argument("--tab", default="Leads")
ap.add_argument("--status_col", default="AD")
ap.add_argument("--only_status", default="amf_none",
                help="only process rows whose status column equals this")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

key = os.getenv("EXA_API_KEY")
if not key:
    raise SystemExit("EXA_API_KEY not set in .claude/.env")


def col(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


SID = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
svc = build("sheets", "v4",
            credentials=Credentials.from_authorized_user_file(TOKEN_PATH))
vals = svc.spreadsheets().values().get(
    spreadsheetId=SID, range=f"{args.tab}!A1:AH3000").execute().get("values", [])
hdr, rows = vals[0], vals[1:]
ix = {x.strip().lower(): i for i, x in enumerate(hdr)}
NM, CITY, DM, ST = ix["company name"], ix["city"], ix["dm name"], col(args.status_col)


def g(r, i):
    return (r[i] if len(r) > i else "").strip()


todo = [{"row": i + 2, "company": g(r, NM), "city": g(r, CITY)}
        for i, r in enumerate(rows)
        if not g(r, DM) and g(r, ST) == args.only_status and g(r, NM)]
if args.limit:
    todo = todo[:args.limit]

print(f"=== Exa DM discovery {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
print(f"rows with no DM and status={args.only_status}: {len(todo)}")
print(f"estimated Exa cost: ~${len(todo)*EXA_COST:.2f}   (names only, no emails)")
if not args.apply:
    print("\n[DRY RUN] nothing spent, nothing written. Re-run with --apply.")
    raise SystemExit

from openai import AzureOpenAI
llm = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                  api_version=AZURE_API_VERSION)

lock = threading.Lock()
stats = Counter()
spend = [0.0]
writes = []


def work(t):
    res, cost, err = exa(t["company"], t["city"], key)
    with lock:
        spend[0] += cost
    if err:
        with lock:
            stats[err] += 1
        return
    name, title, why = extract(llm, t["company"], t["city"], res)
    with lock:
        if name:
            stats["found"] += 1
            parts = name.split()
            writes.append((t["row"], name, title, parts[0], parts[-1]))
            print(f"  ✓ {t['company'][:34]:36}{name:24}{title[:26]}")
        else:
            stats[why or "none"] += 1


with ThreadPoolExecutor(max_workers=args.workers) as pool:
    list(pool.map(work, todo))

data = []
for row, name, title, first, last in writes:
    data += [{"range": f"{args.tab}!T{row}", "values": [[name]]},
             {"range": f"{args.tab}!U{row}", "values": [[title]]},
             {"range": f"{args.tab}!X{row}", "values": [[first]]},
             {"range": f"{args.tab}!Y{row}", "values": [[last]]},
             {"range": f"{args.tab}!{args.status_col}{row}",
              "values": [["dm_exa_found"]]}]
for k in range(0, len(data), 100):
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SID, body={"valueInputOption": "RAW", "data": data[k:k+100]}).execute()

print("\n=== Summary ===")
print(f"  names found : {stats['found']}/{len(todo)} "
      f"({100*stats['found']/max(1,len(todo)):.0f}%)")
print(f"  Exa spend   : ${spend[0]:.2f}")
for k, n in stats.most_common():
    if k != "found":
        print(f"    {k:30} {n}")
print("\nNext: amf_person_fill.py resolves emails for these at 1 credit each.")
