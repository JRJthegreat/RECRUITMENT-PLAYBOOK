"""
Phase 2 - merge every research source into ONE dossier per lead.

Consumes the website facts (scrape_facts.py) and the LinkedIn profile
(scrape_linkedin.py) and produces:

  research_summary   long prose. Everything known about the company AND the
                     person: what they do, who they place, where, how long,
                     the founder's background, published numbers, anything
                     notable. This is the human-readable record.
  primary_niche      controlled vocabulary, used to ROUTE COPY.
  niche_detail       the specific version in their own terms
                     ("allied health and travel nursing, Southeast US")
  healthcare_fit     primary | partial | none | unknown
  best_fact          the single strongest angle for an icebreaker, or NONE

Why niche is a first-class output, not a fact type: the offer line changes per
lead. A firm that places ICU nurses and a firm that places CNC machinists
cannot receive the same "I track employers in your space" email. healthcare_fit
decides whether they get the healthcare copy or an industry-matched variant.

Source-of-truth rule: LinkedIn OVERRIDES the sheet. Profiles routinely show the
person has moved employer since the list was built, and the sheet's company
name is then stale. Flag it rather than writing copy about the old firm.

Batch-of-10 writes. Resume-safe: skips rows where --col_summary is filled.

Run:
  python3 -W ignore build_dossier.py --sheet_url "URL" --tab "TAB" \
    --col_company D --col_website F --col_first B --col_last C --col_title G \
    --col_facts K --col_linkedin_data Q \
    --col_summary R --col_niche S --col_niche_detail T \
    --col_hc_fit U --col_best_fact V [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

WRITE_BATCH = 10
WORKERS     = 6

NICHES = [
    "healthcare_clinical", "healthcare_nonclinical", "it_tech", "engineering_construction",
    "manufacturing_industrial", "finance_accounting", "legal", "sales_marketing",
    "hr_talent", "logistics_supply_chain", "education", "hospitality_retail",
    "life_sciences_pharma", "government_defense", "executive_general", "generalist",
    "not_a_recruiter", "unknown",
]

SYSTEM = (
    "You are a B2B lead researcher. You are given everything we could gather about a "
    "small recruitment/staffing agency and the person we intend to email. Produce a "
    "single consolidated dossier.\n\n"
    "Return STRICT JSON with these keys:\n"
    "  research_summary  (string) Detailed prose, 120-220 words. Cover, in this order: "
    "what the company does and who it places, the specific verticals and role types, "
    "geography, company size and age, then the PERSON: their title, tenure, career "
    "before this firm, education, anything they publish about themselves. Include every "
    "concrete number you find. If a source contradicts another, say so. If something is "
    "unknown, say it is unknown rather than padding. No marketing language.\n"
    "  primary_niche     (string) EXACTLY one of: " + ", ".join(NICHES) + "\n"
    "  niche_detail      (string) The specific niche in concrete terms, e.g. "
    "\"travel nursing and allied health, Southeast US\" or \"CNC machinists and plant "
    "supervisors, Midwest\". Empty string if genuinely unclear.\n"
    "  roles_placed      (string) Actual job titles they recruit for, comma separated. "
    "Empty if unknown.\n"
    "  geography         (string) Where they operate. Empty if unknown.\n"
    "  healthcare_fit    (string) one of: primary (healthcare is their main business), "
    "partial (they do some healthcare among other verticals), none (no healthcare "
    "evidence), unknown (not enough information).\n"
    "  second_fact       (string) A DIFFERENT concrete specific, unrelated to best_fact, "
    "for the second half of the icebreaker. Prefer a SMALL NON-OBVIOUS detail over a "
    "headline claim: a named division, a named association membership, an unusual "
    "service line, a specific role type, a named location. The homepage banner stat is "
    "what everyone would cite, so prefer something buried. NEVER education (degrees, "
    "universities, study abroad): the same ban as best_fact applies here. Empty string "
    "if there is no genuine second specific.\n"
    "  best_fact_when    (string) one of: prior_career (happened before their current "
    "firm), current_company (true of the firm or role today), dated_event (a specific "
    "dated happening: press item, award year, partnership, launch), unknown.\n"
    "  second_fact_when  (string) same vocabulary, for second_fact. Empty if second_fact "
    "is empty.\n"
    "  best_fact         (string) The single strongest fact for a cold-email opener, or "
    "the literal string NONE. Strict preference order:\n"
    "     1. The person's professional career BEFORE this firm (e.g. 24 years as an RN, "
    "former lawyer, ran a desk at a named firm).\n"
    "     2. A published number: placements made, retention or fill rate, years in "
    "business, client count.\n"
    "     3. A named award or ranking (Inc 5000, Best of Staffing) with the count/year.\n"
    "     4. A concrete milestone (founding year, a named division launch, expansion).\n"
    "     5. A genuinely narrow specialism plus geography.\n"
    "  NEVER choose EDUCATION (degrees, universities, study abroad) unless literally "
    "nothing else exists. Opening a cold email to a company owner with their undergraduate "
    "degree is weak and slightly insulting. A company-level achievement always beats a "
    "personal educational credential.\n"
    "  It must be concrete and checkable. Do NOT choose values/culture statements, "
    "business-model facts, menial early jobs, or anything undignified.\n"
    "  best_fact_type    (string) one of: founder_background, published_numbers, awards, "
    "milestone, credentials, niche, none\n"
    "  employer_changed  (boolean) true if LinkedIn shows the person now works somewhere "
    "OTHER than the company on file.\n"
    "  current_employer  (string) Their actual current employer per LinkedIn, if it "
    "differs from the company on file. Else empty.\n\n"
    "RECENCY\n"
    "Today's date is <TODAY>. A dated fact from the last 6 months (a press item, "
    "partnership, award, expansion, anniversary) is the single most valuable icebreaker "
    "material there is, because it proves the reader was researched THIS month rather "
    "than scraped once long ago. When one exists, it should be best_fact or second_fact "
    "even if the standard preference order says otherwise.\n\n"
    "RULES\n"
    "- LinkedIn is the source of truth over the sheet. People change jobs and our list "
    "goes stale.\n"
    "- Never invent. Absent evidence, use empty string / unknown / NONE.\n"
    "- 'not_a_recruiter' is a valid and useful answer: some rows are software vendors, "
    "consultancies or unrelated businesses.\n"
    "- Judge niche on what they actually RECRUIT FOR, not their own industry label."
)


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
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


def scrub(s):
    s = str(s or "").strip().replace("—", ", ").replace("–", ", ")
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("…", "...")]:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


from datetime import date


def build_input(p):
    parts = [
        "=== ON FILE (may be stale) ===",
        f"person   : {p['first']} {p['last']}".rstrip(),
        f"title    : {p['title'] or 'unknown'}",
        f"company  : {p['company'] or 'unknown'}",
        f"website  : {p['website']}",
    ]
    if p["facts"]:
        try:
            f = json.loads(p["facts"])
            if isinstance(f, dict) and "pages" in f:
                # New format: one abstract per crawled page. Keep them labelled
                # by URL so the synthesiser can tell a team bio from a homepage
                # banner, and prefer the buried specifics over the headline.
                parts += ["", f"=== WEBSITE ({f.get('n', 0)} pages read) ==="]
                for pg in f.get("pages", []):
                    parts += [f"--- {pg.get('url','')}", pg.get("abstract", "")]
                if not f.get("pages"):
                    parts += ["(site unreachable or boilerplate only)"]
            else:
                nz = {k: v for k, v in f.items() if v}
                parts += ["", "=== WEBSITE SCRAPE ===",
                          json.dumps(nz, ensure_ascii=False) if nz else "(nothing concrete found)"]
        except Exception:
            pass
    if p["li"]:
        try:
            parts += ["", "=== LINKEDIN PROFILE ===",
                      json.dumps(json.loads(p["li"]), ensure_ascii=False)[:3500]]
        except Exception:
            pass
    return "\n".join(parts)


def synth(client, p):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=800, temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM.replace("<TODAY>", date.today().isoformat())},
                      {"role": "user", "content": build_input(p)}],
        )
        d = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        return p["row"], None, str(e)[:80]
    out = {
        "research_summary": scrub(d.get("research_summary"))[:2000],
        "primary_niche": scrub(d.get("primary_niche")).lower()[:40],
        "niche_detail": scrub(d.get("niche_detail"))[:200],
        "roles_placed": scrub(d.get("roles_placed"))[:200],
        "geography": scrub(d.get("geography"))[:120],
        "healthcare_fit": scrub(d.get("healthcare_fit")).lower()[:20],
        "best_fact": scrub(d.get("best_fact"))[:300],
        "second_fact": scrub(d.get("second_fact"))[:300],
        "best_fact_when": scrub(d.get("best_fact_when")).lower()[:20],
        "second_fact_when": scrub(d.get("second_fact_when")).lower()[:20],
        "best_fact_type": scrub(d.get("best_fact_type")).lower()[:30],
        "employer_changed": bool(d.get("employer_changed")),
        "current_employer": scrub(d.get("current_employer"))[:120],
    }
    if out["primary_niche"] not in NICHES:
        out["primary_niche"] = "unknown"
    return p["row"], out, None


def ensure_cols(service, sheet_id, tab, cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sh = next(s for s in meta["sheets"] if s["properties"]["title"] == tab)
    need = max(c for c, _ in cols) + 1
    cur = sh["properties"]["gridProperties"]["columnCount"]
    if cur < need:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sh["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": need - cur}}]}).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab}'!{col_letter(c)}1", "values": [[h]]} for c, h in cols]}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--col_company", default="D")
    ap.add_argument("--col_website", default="F")
    ap.add_argument("--col_first", default="B")
    ap.add_argument("--col_last", default="C")
    ap.add_argument("--col_title", default="G")
    ap.add_argument("--col_facts", default="K")
    ap.add_argument("--col_linkedin_data", default="Q")
    ap.add_argument("--col_summary", default="R")
    ap.add_argument("--col_niche", default="S")
    ap.add_argument("--col_niche_detail", default="T")
    ap.add_argument("--col_hc_fit", default="U")
    ap.add_argument("--col_best_fact", default="V")
    ap.add_argument("--col_roles", default="W")
    ap.add_argument("--col_flags", default="X")
    ap.add_argument("--col_second_fact", default="Y")
    ap.add_argument("--col_best_when", default="Z")
    ap.add_argument("--col_second_when", default="AA")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--preview", type=int, default=0)
    a = ap.parse_args()

    sheet_id = parse_sheet_id(a.sheet_url)
    tab = a.tab
    C = {k: col_to_idx(v) for k, v in {
        "company": a.col_company, "website": a.col_website, "first": a.col_first,
        "last": a.col_last, "title": a.col_title, "facts": a.col_facts,
        "li": a.col_linkedin_data, "summary": a.col_summary, "niche": a.col_niche,
        "niche_detail": a.col_niche_detail, "hc": a.col_hc_fit,
        "best": a.col_best_fact, "roles": a.col_roles, "flags": a.col_flags,
        "second": a.col_second_fact, "bwhen": a.col_best_when, "swhen": a.col_second_when,
    }.items()}

    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)
    service = get_service()
    outcols = [(C["summary"], "research_summary"), (C["niche"], "primary_niche"),
               (C["niche_detail"], "niche_detail"), (C["hc"], "healthcare_fit"),
               (C["best"], "best_fact"), (C["roles"], "roles_placed"),
               (C["flags"], "flags"), (C["second"], "second_fact"),
               (C["bwhen"], "best_fact_when"), (C["swhen"], "second_fact_when")]
    if not a.preview:
        ensure_cols(service, sheet_id, tab, outcols)

    last = col_letter(max(C.values()))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last}").execute().get("values", [])[1:]

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    pending = []
    for i, r in enumerate(rows):
        if cell(r, C["summary"]):
            continue
        facts, li = cell(r, C["facts"]), cell(r, C["li"])
        if not facts and not li:
            continue          # nothing to synthesize from
        pending.append({
            "row": i + 2, "first": cell(r, C["first"]), "last": cell(r, C["last"]),
            "title": cell(r, C["title"]), "company": cell(r, C["company"]),
            "website": cell(r, C["website"]), "facts": facts, "li": li,
        })

    if a.limit:
        pending = pending[:a.limit]
    if a.preview:
        pending = pending[:a.preview]

    print(f"=== Build Dossier ===\nRows with research to synthesize: {len(pending)}\n")
    if not pending:
        print("Nothing to do. Run scrape_facts.py / scrape_linkedin.py first.")
        return

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(synth, client, p) for p in pending]
        for fut in as_completed(futs):
            results.append(fut.result())

    if a.preview:
        from collections import Counter
        for row, d, err in sorted(results, key=lambda x: x[0]):
            print(f"--- Row {row} ---")
            if err or not d:
                print(f"  ERROR {err}")
                continue
            print(f"  niche      : {d['primary_niche']}  |  hc_fit: {d['healthcare_fit']}")
            print(f"  detail     : {d['niche_detail']}")
            print(f"  roles      : {d['roles_placed']}")
            print(f"  geography  : {d['geography']}")
            if d["employer_changed"]:
                print(f"  ** MOVED   : now at {d['current_employer']}")
            print(f"  best_fact  : [{d['best_fact_type']}] {d['best_fact'][:150]}")
            print(f"  second     : {d.get('second_fact','')[:150]}")
            print(f"  summary    : {d['research_summary'][:500]}")
            print()
        ok = [d for _, d, e in results if d]
        print("=== NICHE MIX ===")
        for k, v in Counter(d["primary_niche"] for d in ok).most_common():
            print(f"  {k:28s} {v}")
        print("=== HEALTHCARE FIT ===")
        for k, v in Counter(d["healthcare_fit"] for d in ok).most_common():
            print(f"  {k:28s} {v}")
        return

    updates = []
    for row, d, err in sorted(results, key=lambda x: x[0]):
        if not d:
            continue
        flags = []
        if d["employer_changed"] and d["current_employer"]:
            flags.append(f"MOVED->{d['current_employer']}")
        if d["primary_niche"] == "not_a_recruiter":
            flags.append("NOT_A_RECRUITER")
        updates.append((row, [
            (C["summary"], d["research_summary"]),
            (C["niche"], d["primary_niche"]),
            (C["niche_detail"], d["niche_detail"]),
            (C["hc"], d["healthcare_fit"]),
            (C["best"], "" if d["best_fact"].upper() == "NONE" else d["best_fact"]),
            (C["roles"], d["roles_placed"]),
            (C["flags"], "; ".join(flags)),
            (C["second"], d.get("second_fact", "")),
            (C["bwhen"], d.get("best_fact_when", "")),
            (C["swhen"], d.get("second_fact_when", "")),
        ]))
        if len(updates) >= WRITE_BATCH:
            _flush(service, sheet_id, tab, updates)
            updates = []
    if updates:
        _flush(service, sheet_id, tab, updates)

    ok = sum(1 for _, d, _ in results if d)
    print(f"\nDone - {ok}/{len(pending)} dossiers written.")


def _flush(service, sheet_id, tab, updates):
    data = []
    for row, pairs in updates:
        for c, v in pairs:
            data.append({"range": f"'{tab}'!{col_letter(c)}{row}", "values": [[v]]})
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
            break
        except Exception as e:
            # 60 writes/min/user quota: six LLM workers outrun one write/sec,
            # so back off and retry instead of dying mid-run.
            if attempt < 3 and "429" in str(e):
                time.sleep(65)
            else:
                raise
    print(f"  -> wrote {len(updates)} rows", flush=True)
    time.sleep(1.1)


if __name__ == "__main__":
    main()
