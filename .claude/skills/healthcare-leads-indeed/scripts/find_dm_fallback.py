"""
Fallback DM finder for opening-tab rows that still have NO decision maker after the
primary CEO/Owner pass.

Authority ladder (highest first): COO/VP-Dir Operations -> Medical Director ->
Practice Administrator/Manager/Office Manager -> HR/People. One Google search ORs
all these titles; the highest-authority VALID match wins. If Google finds nothing,
AMF /decision-maker is tried with categories coo -> operations -> hr (using the
company domain when present, else the company name).

Only processes rows where col T (DM Name) is empty. Writes T/U/V (+ W email when AMF
returns one). Dedupe by company, batch sheet writes.

Usage:
  python3 -W ignore find_dm_fallback.py --sheet_url "URL" --tab "Single Opening" [--dry_run]
"""

import os
import re
import time
import argparse
import requests
from collections import OrderedDict
from urllib.parse import urlparse
from dotenv import load_dotenv

from pull_dataset import get_google_service, get_sheet_id_from_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
AMF_KEY     = os.getenv("ANYMAILFINDER_API_KEY")
APIFY_BASE  = "https://api.apify.com/v2"
AMF_DM_URL  = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"

COL_CO=10; COL_WEBSITE=11; COL_DM_NAME=19; COL_DM_TITLE=20; COL_DM_LI=21; COL_EMAIL=22
BATCH = 10

FALLBACK_QUERY = ('"COO" OR "Chief Operating Officer" OR "VP of Operations" '
                  'OR "Vice President of Operations" OR "Director of Operations" '
                  'OR "Medical Director" OR "Practice Administrator" OR "Practice Manager" '
                  'OR "Office Manager" OR "Clinic Manager" OR "HR Manager" '
                  'OR "Director of Human Resources" OR "Director of People" OR "Head of People"')

# tier -> ordered keyword list (lower tier = higher authority)
TIER_KEYWORDS = [
    (1, ("coo", "chief operating officer", "vp of operations", "vice president of operations",
         "vp operations", "director of operations", "head of operations")),
    (2, ("medical director", "chief medical officer", "cmo")),
    (3, ("practice administrator", "practice manager", "office manager", "clinic manager",
         "practice director", "administrator")),
    (4, ("hr manager", "director of human resources", "human resources", "director of people",
         "head of people", "people operations", "chief people")),
]
AMF_CATEGORIES = ["coo", "operations", "hr"]

EA_REJECT = ("assistant to", "executive assistant", "administrative assistant", "assistant office")
FORMER_RE = re.compile(r"\b(former|formerly|ex[\-\s]|previously|past)\b", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)
NOISE = {"inc","llc","ltd","corp","co","the","of","and","&","a","an","for","in","at","by",
         "group","services","company","pllc","pc","pa"}


def cell(r, i): return r[i].strip() if i < len(r) and r[i] else ""
def col_letter(idx):
    s=""; idx+=1
    while idx: idx,rem=divmod(idx-1,26); s=chr(65+rem)+s
    return s
def name_words(t): return [w for w in re.split(r"[\s,.\-&/()+]+", (t or "").lower()) if len(w)>2 and w not in NOISE]


def domain_from(site):
    w=(site or "").strip().lower()
    if not w: return ""
    if "://" not in w: w="http://"+w
    net=urlparse(w).netloc
    return net[4:] if net.startswith("www.") else net


def tier_of(title):
    t=(title or "").lower().strip()
    if not t: return 99
    if any(x in t for x in EA_REJECT): return 99
    for tier, kws in TIER_KEYWORDS:
        for kw in kws:
            if re.search(r"(?<![a-z])"+re.escape(kw)+r"(?![a-z])", t):
                return tier
    return 99


def parse_li(org):
    tf=org.get("title","") or ""; desc=org.get("description","") or ""; url=org.get("url","") or ""
    m=LINKEDIN_RE.search(url)
    if not m: return None
    slug=m.group(1).lower()
    if not slug or len(slug)<3: return None
    tc=re.sub(r"\s*\|\s*LinkedIn\s*$","",tf,flags=re.IGNORECASE).strip()
    parts=re.split(r"\s*[\-–—|·]\s*",tc,maxsplit=2)
    name=parts[0].strip() if parts else ""
    title=co=""
    if len(parts)>=2:
        chunk=parts[1].strip()
        mat=re.search(r"^(.*?)\s+at\s+(.+)$",chunk,re.IGNORECASE)
        if mat: title,co=mat.group(1).strip(),mat.group(2).strip()
        else: title=chunk
    if len(parts)>=3 and not co: co=parts[2].strip()
    return {"name":name,"title":title,"company":co,"snippet":desc,"url":url}


def company_overlap(snippet_company, target):
    a=set(name_words(snippet_company)); b=set(name_words(target))
    if not b: return 1.0
    long_b={w for w in b if len(w)>=5}
    if long_b: return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a&b else 0.0


def snippet_overlap(snippet, target):
    a=set(re.split(r"[\s,.\-&/()+]+",(snippet or "").lower())); b=set(name_words(target))
    if not b: return 1.0
    long_b={w for w in b if len(w)>=5}
    if long_b: return 1.0 if long_b.issubset(a) else 0.0
    return 1.0 if a&b else 0.0


def valid(parsed, company):
    if not parsed or not parsed["name"] or not parsed["title"]: return False
    if FORMER_RE.search(parsed["title"]): return False
    if tier_of(parsed["title"]) == 99: return False
    co=parsed["company"].strip(".… \t") if parsed["company"] else ""
    if co and len(co)>=3:
        return company_overlap(co, company) >= 1.0
    tl={w for w in name_words(company) if len(w)>=5}
    return bool(tl) and snippet_overlap(parsed["snippet"], company) >= 1.0


def apify_search(queries):
    r=requests.post(f"{APIFY_BASE}/acts/apify~google-search-scraper/run-sync-get-dataset-items",
        params={"token":APIFY_TOKEN},
        json={"queries":"\n".join(queries),"resultsPerPage":8,"maxPagesPerQuery":1,
              "languageCode":"en","countryCode":"us","includeUnfilteredResults":False},
        timeout=300)
    if r.status_code not in (200,201): return {}
    out={}
    for item in r.json():
        q=item.get("searchQuery",{}).get("term","")
        if q: out[q]=item.get("organicResults",[])
    return out


def amf_dm(domain, company):
    headers={"Authorization":AMF_KEY,"Content-Type":"application/json"}
    for cat in AMF_CATEGORIES:
        try:
            body={"decision_maker_category":[cat]}
            if domain: body["domain"]=domain
            if company: body["company_name"]=company
            r=requests.post(AMF_DM_URL,headers=headers,json=body,timeout=60)
            r.raise_for_status()
            d=r.json(); name=d.get("person_full_name","") or ""
            if name:
                em=d.get("valid_email") or d.get("email"); st=d.get("email_status","")
                return {"name":name,"title":d.get("person_job_title","") or "",
                        "url":d.get("person_linkedin_url","") or "",
                        "email":em if em and st == "valid" else None}
        except Exception:
            pass
    return None


def main():
    ap=argparse.ArgumentParser(description="Fallback DM finder (COO/Medical Dir/Practice Admin/HR)")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args=ap.parse_args()

    svc=get_google_service(); sid=get_sheet_id_from_url(args.sheet_url)
    print(f"=== Fallback DM Finder {'[DRY RUN]' if args.dry_run else ''} — {args.tab} ===\n")

    rows=svc.spreadsheets().values().get(spreadsheetId=sid,range=f"'{args.tab}'!A2:W5000").execute().get("values",[])
    companies=OrderedDict()
    for i,r in enumerate(rows):
        co=cell(r,COL_CO); dm=cell(r,COL_DM_NAME)
        if not co or dm: continue
        if co not in companies:
            companies[co]={"name":co,"domain":domain_from(cell(r,COL_WEBSITE)),"rows":[]}
        companies[co]["rows"].append(i+2)
        if not companies[co]["domain"]: companies[co]["domain"]=domain_from(cell(r,COL_WEBSITE))

    todo=list(companies.values())
    print(f"Companies still missing a DM: {len(todo)}")
    if args.dry_run:
        for g in todo[:10]:
            print(f"  {g['name'][:40]:40s} domain={g['domain']}")
        print("\n[DRY RUN] No calls.")
        return

    found_g=found_amf=miss=0
    for start in range(0,len(todo),BATCH):
        batch=todo[start:start+BATCH]
        q_map={}
        for g in batch:
            clause=f'("{g["name"]}" OR "{g["domain"]}")' if g["domain"] else f'"{g["name"]}"'
            q=f'{clause} ({FALLBACK_QUERY}) site:linkedin.com/in/'
            q_map[q]=g
        raw=apify_search(list(q_map))

        updates=[]
        for q,g in q_map.items():
            best=None; best_tier=99
            for org in raw.get(q,[]):
                p=parse_li(org)
                if valid(p, g["name"]):
                    t=tier_of(p["title"])
                    if t<best_tier:
                        best_tier=t; best=p
            email=None
            if best:
                found_g+=1
                print(f"  ✓[G t{best_tier}] {g['name'][:32]:32s} -> {best['name']} ({best['title'][:30]})")
                hit=best
            else:
                amf=amf_dm(g["domain"], g["name"]) if (g["domain"] or g["name"]) else None
                if amf:
                    found_amf+=1; hit=amf; email=amf.get("email")
                    print(f"  ✓[AMF]   {g['name'][:32]:32s} -> {amf['name']} ({amf['title'][:30]})")
                else:
                    miss+=1
                    print(f"  ✗        {g['name'][:32]:32s} -> still no match")
                    continue
            for rn in g["rows"]:
                updates+=[
                    {"range":f"'{args.tab}'!{col_letter(COL_DM_NAME)}{rn}","values":[[hit['name']]]},
                    {"range":f"'{args.tab}'!{col_letter(COL_DM_TITLE)}{rn}","values":[[hit.get('title','')]]},
                    {"range":f"'{args.tab}'!{col_letter(COL_DM_LI)}{rn}","values":[[hit.get('url','')]]},
                ]
                if email:
                    updates.append({"range":f"'{args.tab}'!{col_letter(COL_EMAIL)}{rn}","values":[[email]]})
        if updates:
            for attempt in range(4):
                try:
                    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid,
                        body={"valueInputOption":"RAW","data":updates}).execute()
                    break
                except Exception:
                    if attempt<3: time.sleep(4)
        print(f"  -- batch {start//BATCH+1}: G={found_g} AMF={found_amf} miss={miss} --")
        time.sleep(1)

    print(f"\n=== Done === Google={found_g}, AMF={found_amf}, still missing={miss}  (of {len(todo)})")


if __name__ == "__main__":
    main()
