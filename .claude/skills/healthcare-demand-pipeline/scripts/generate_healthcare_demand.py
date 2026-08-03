"""
Phase 4 — Healthcare Clinical Demand campaign: generate per-lead variables +
assembled body (personalization) per the approved copy framework (Google Doc
"Healthcare Campaign Copy Framework" v4 + section 9).

Per lead (status starts with 'found', email in W, not skip_*):
  - Persona from DM title (code): A clinical leader / B owner-CEO / C top HR.
  - Age band from Date Published (code): 30-60d / 8-29d / 0-7d opener.
  - GPT-4.1 (one JSON call, controlled lists only): cleaned_role, role_plural,
    employer_type (org + optional people form), team_word, casual_company,
    duration_stat.
  - Body assembled deterministically. NO sign-off (the Instantly sequence
    appends "Best, {{sendingAccountFirstName}}").

Writes: Z body, AD persona, AE age_band, AF cleaned_role, AG role_plural,
AH team_word, AI employer_type, AJ month, AK casual_company.
Batch-of-10 writes; idempotent (skips rows where AF already set).

Usage:
  python3 -W ignore generate_healthcare_demand.py --sheet_url "URL" --preview 6
  python3 -W ignore generate_healthcare_demand.py --sheet_url "URL"   # real run
"""

import os
import re
import json
import argparse
import threading
from datetime import date
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")
AZURE_REVIEWER = os.getenv("AZURE_OPENAI_DEPLOYMENT", AZURE_DEPLOYMENT)  # GPT-5.1

TAB = "Leads"
BATCH = 10
WORKERS = 8

# --- Column indices (0-based) ---
C_TITLE, C_DATE, C_COMPANY, C_SIZE = 1, 4, 10, 12
C_CITY, C_STATE = 17, 18
C_DM_TITLE, C_EMAIL, C_FIRST = 20, 22, 23
C_BODY, C_STATUS = 25, 27
C_PERSONA, C_AGE, C_CROLE, C_RPLUR, C_TEAM, C_ETYPE, C_MONTH, C_CCOMP, C_REVIEW, C_CFIRST, C_CCITY = (
    29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39)  # AD..AN

TODAY = date.today()

EMPLOYER_TYPES = [
    "community health centers", "private medical practices", "dental practices",
    "hospitals", "health systems", "home health agencies",
    "senior living communities", "outpatient therapy clinics",
    "independent pharmacies", "imaging centers", "behavioral health clinics",
    "urgent care clinics", "surgery centers", "medical labs", "EMS providers",
    "schools", "healthcare employers",
]
PEOPLE_FORMS = ["practice owners", "dental practice owners", "clinic owners",
                "pharmacy owners"]
TEAM_WORDS = ["unit", "practice", "clinic", "department", "pharmacy", "lab",
              "special-ed team", "team"]
DURATION_STATS = ["2-3 months", "60-90 days", "3+ months"]

SYSTEM = f"""You prepare personalization variables for a cold email to a healthcare
employer that posted a job. Reply with ONLY JSON.

Fields (every value MUST come from the rules/lists — never invent):
- cleaned_role: the job title as a hiring manager says it out loud. Specialty
  leads, level qualifies it. Strip locations, shifts, schedules, PRN/FT/PT tags,
  parentheticals, req IDs, sign-on-bonus fluff.
  "Staff Physical Therapist - Oak Park" -> "Staff Physical Therapist"
  "Registered Nurse - Progressive Care Unit - Full Time Nights" -> "Progressive Care RN"
  "RN - ICU (Weekend Option) $10k Sign-On" -> "ICU RN"
  "Speech Language Pathologist (SLP) - Schools" -> "Speech Language Pathologist"
- role_plural: casual plural of cleaned_role ("ICU RNs", "NPs", "dental hygienists",
  "speech-language pathologists", "rad techs").
- employer_type_org: from {json.dumps(EMPLOYER_TYPES)}. Pick a specific type
  ONLY when it is unmistakable from the company name alone (e.g. "...Dental" ->
  dental practices, "...Pharmacy" -> independent pharmacies, "...Hospital" ->
  hospitals). If there is ANY ambiguity, use "healthcare employers". A generic
  correct line beats a specific wrong one.
- employer_type_people: from {json.dumps(PEOPLE_FORMS)} ONLY if the company is
  clearly a small owner-run shop matching one of those; else null.
- team_word: from {json.dumps(TEAM_WORDS)} — where this vacancy's pain lands.
  MUST match the company type: hospital/health system nursing -> "unit";
  hospital non-nursing -> "department"; medical/dental office -> "practice";
  therapy/urgent care -> "clinic"; pharmacy -> "pharmacy"; lab -> "lab";
  school -> "special-ed team"; home health, EMS, or anything ambiguous -> "team".
- casual_company: company name as said in conversation. Strip legal suffixes
  (Inc, LLC, PC), generic tails when natural. "Sullivan County Community
  Hospital, Inc." -> "Sullivan County Community Hospital". Keep recognizable.
- casual_first: the DM's first name as a colleague says it. Common nicknames
  only: "William" -> "Will", "Jennifer" -> "Jen", "Michael" -> "Mike".
  Keep the original when no common nickname exists. Never invent.
- casual_city: the job city as locals say it: "Indianapolis" -> "Indy",
  "Philadelphia" -> "Philly". Keep the original when no common nickname exists.
- duration_stat: from {json.dumps(DURATION_STATS)} — RN/clinical "2-3 months",
  dental hygienist "60-90 days", surgical tech "3+ months", default "2-3 months".
- in_scope: true ONLY if this is a clinical or allied-health role a healthcare
  recruiter places: physician, NP, PA, nursing, CRNA, therapy (PT/OT/SLP),
  pharmacy, imaging/radiology, lab, dental, respiratory, behavioral health,
  EMS. false for anything else (research scientists, front desk, admin, sales,
  IT, fitness, food service) or if the company is clearly not a healthcare or
  school employer.
"""

MIDDLE = ("I connect {etype} with specialist clinical recruiting firms, and I know "
          "a healthcare recruiter who sources fast. I just spoke on the phone with him "
          "and he said he can put pre-vetted {rplur} in front of you within 72 hours. "
          "No upfront commitment whatsoever, and unlike most recruiters, if a hire "
          "doesn't stick within the first 30 days you get a refund.")
MIDDLE_C = ("I connect {etype} with specialist clinical recruiting firms, and I know "
            "a healthcare recruiter who sources fast. I just spoke on the phone with him "
            "and he said he can put pre-vetted {rplur} in front of you within 72 hours. "
            "He works within your existing vendor process, there's no upfront commitment, "
            "and unlike most recruiters, if a hire doesn't stick within the first 30 days "
            "you get a refund.")
CTA = ("If you're still looking to hire for this role, I can make a quick intro by "
       "CC'ing him on this thread so you can discuss directly.\n\nShould I make the intro?")

REVIEW_SYSTEM = """You are reviewing an assembled cold email for mechanical errors
before it is sent to a real healthcare decision maker. The email was built from
templates with inserted variables; your ONLY job is to catch and fix insertion
defects. You are NOT a copywriter here.

Fix ONLY:
- Grammar breaks from insertion: a/an agreement ("a ICU RN" -> "an ICU RN"),
  duplicated words, broken plurals, capitalization at sentence starts.
- Nonsense from a mis-picked variable: an employer type that contradicts or
  over-specifies the company (when in doubt, replace with "healthcare
  employers"), or a pain/team word that does not match the company type
  ("revenue the department never gets back" about a small practice -> "practice";
  "the unit" outside hospital nursing -> a fitting word or "team").
- Placeholder leftovers like {anything} or empty slots.

NEVER:
- Change these claims in any way: "72 hours", "30 days", "no upfront
  commitment", the refund.
- Change the CTA lines or "Should I make the intro?".
- Add em dashes. Never add new sentences or facts. Never change the tone.
- Rewrite anything that is already correct.

Reply ONLY JSON: {"ok": true} if the email is clean, or
{"ok": false, "fixed_body": "<the corrected email, minimal edits>"}"""

REQUIRED = ["72 hours", "30 days", "Should I make the intro?"]


def review_body(client, body, company, cleaned_role):
    try:
        resp = client.chat.completions.create(
            model=AZURE_REVIEWER, max_completion_tokens=2000,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": REVIEW_SYSTEM},
                      {"role": "user",
                       "content": f'Company: "{company}" | Role: "{cleaned_role}"\n\n{body}'}])
        v = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"  [!] review {company}: {type(e).__name__}")
        return body, "review_error_kept_original"
    if v.get("ok"):
        return body, "ok"
    fixed = (v.get("fixed_body") or "").strip()
    # guardrails: reviewer output must preserve claims/CTA and stay em-dash free
    if (fixed and all(k in fixed for k in REQUIRED)
            and "—" not in fixed and "{" not in fixed):
        return fixed, "fixed"
    return body, "review_rejected_kept_original"


CLINICAL_RE = re.compile(
    r"medical director|chief medical|clinical director|chief clinical|"
    r"director of nursing|chief nursing|cno\b|director of pharmacy|"
    r"director of radiology|director of imaging|director of rehab|"
    r"director of therapy|laboratory director|director of clinical|"
    r"director of provider|dir(ector)? of (patient|resident) care", re.I)
HR_RE = re.compile(r"chro|chief people|chief human|human resources|talent", re.I)


def persona_for(dm_title):
    t = dm_title or ""
    if CLINICAL_RE.search(t):
        return "A"
    if HR_RE.search(t):
        return "C"
    return "B"


def age_band(date_pub):
    try:
        d = (TODAY - date.fromisoformat((date_pub or "")[:10])).days
    except ValueError:
        return "30-60", 45
    if d <= 7:
        return "0-7", d
    if d <= 29:
        return "8-29", d
    return "30-60", d


MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def month_name(date_pub):
    try:
        return MONTHS[int(date_pub[5:7])]
    except (ValueError, IndexError):
        return "May"


def build_opener(persona, band, v, city, month):
    city = v.get("casual_city") or city
    cr, tw, comp, ds = v["cleaned_role"], v["team_word"], v["casual_company"], v["duration_stat"]
    if band == "8-29":
        return (f"Saw your {cr} posting in {city} went up a few weeks back. These roles "
                f"are staying unfilled for {ds} nationally right now, and I'm guessing "
                f"the hiring is mostly riding on inbound applicant flow, which makes the "
                f"timeline hard to predict.")
    if band == "0-7":
        return (f"Saw {comp} just posted for a {cr} in {city}. Heads up from someone who "
                f"watches this market: these roles are staying unfilled {ds} nationally, "
                f"and inbound applicant flow tends to bury you in unqualified resumes "
                f"before it surfaces the right one.")
    if persona == "B":
        return (f"Noticed your {cr} posting in {city} has been up since {month}. Every "
                f"week that seat sits empty is revenue the {tw} never gets back.")
    if persona == "C":
        return (f"Your team has a {cr} req open in {city} since {month}, and I'd bet it's "
                f"one of a dozen clinical searches competing for their time. This isn't a "
                f"pitch to replace anyone's work internally.")
    return (f"Saw {comp} has had a {cr} role open in {city} since {month}. I know what "
            f"that usually means for the {tw}, more overtime, more agency shifts, "
            f"everyone stretched a little thinner.")


def build_body(persona, band, v, city, month, first):
    first = v.get("casual_first") or first
    opener = build_opener(persona, band, v, city, month)
    etype = (v.get("employer_type_people") if persona == "B" and v.get("employer_type_people")
             else v["employer_type_org"])
    middle = (MIDDLE_C if persona == "C" else MIDDLE).format(
        etype=etype, rplur=v["role_plural"])
    return f"Hi {first},\n\n{opener}\n\n{middle}\n\n{CTA}", etype


def get_vars(client, lead):
    user = (f'Company: "{lead["company"]}"\nJob title: "{lead["job_title"]}"\n'
            f'DM first name: "{lead["first"]}"\n'
            f'Location: {lead["city"]}, {lead["state"]}\nCompany size: {lead["size"] or "unknown"}')
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=250, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}])
        v = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"  [!] LLM {lead['company']}: {type(e).__name__}")
        return None
    # validate against controlled lists; fall back rather than guess
    if v.get("employer_type_org") not in EMPLOYER_TYPES:
        v["employer_type_org"] = "healthcare employers"
    if v.get("employer_type_people") not in PEOPLE_FORMS:
        v["employer_type_people"] = None
    if v.get("team_word") not in TEAM_WORDS:
        v["team_word"] = "team"
    if v.get("duration_stat") not in DURATION_STATS:
        v["duration_stat"] = "2-3 months"
    if not v.get("cleaned_role"):
        v["cleaned_role"] = lead["job_title"]
    if not v.get("role_plural"):
        v["role_plural"] = f'{v["cleaned_role"]} candidates'
    if not v.get("casual_company"):
        v["casual_company"] = lead["company"]
    if not v.get("casual_first"):
        v["casual_first"] = lead["first"]
    if not v.get("casual_city"):
        v["casual_city"] = lead["city"]
    return v


def out_of_scope(v):
    return v.get("in_scope") is False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = all eligible")
    ap.add_argument("--preview", type=int, default=0,
                    help="Render N examples (spread across personas), no writes")
    args = ap.parse_args()

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url)
    sheet_id = m.group(1)
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    svc = build("sheets", "v4", credentials=creds)

    rows = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TAB}!A1:AN").execute().get("values", [])
    header, data = rows[0], rows[1:]

    def cell(r, i):
        return r[i].strip() if i < len(r) and r[i] else ""

    # headers for new cols
    HDRS = {C_PERSONA: "persona", C_AGE: "age_band", C_CROLE: "cleaned_role",
            C_RPLUR: "role_plural", C_TEAM: "team_word", C_ETYPE: "employer_type",
            C_MONTH: "month", C_CCOMP: "casual_company", C_REVIEW: "review_status",
            C_CFIRST: "casual_first", C_CCITY: "casual_city"}
    if not args.preview:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == TAB)
        if sheet["properties"]["gridProperties"]["columnCount"] < C_CCITY + 1:
            svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
                "requests": [{"appendDimension": {
                    "sheetId": sheet["properties"]["sheetId"],
                    "dimension": "COLUMNS",
                    "length": C_CCITY + 1 - sheet["properties"]["gridProperties"]["columnCount"]}}]}
            ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{TAB}!AD1:AN1", valueInputOption="RAW",
            body={"values": [[HDRS[i] for i in range(C_PERSONA, C_CCITY + 1)]]}).execute()

    todo = []
    for i, r in enumerate(data):
        status = cell(r, C_STATUS)
        if not status.startswith("found") or not cell(r, C_EMAIL):
            continue
        if cell(r, C_CROLE):  # already generated
            continue
        todo.append({
            "row": i + 2, "company": cell(r, C_COMPANY),
            "job_title": cell(r, C_TITLE), "date": cell(r, C_DATE),
            "size": cell(r, C_SIZE), "city": cell(r, C_CITY) or "your area",
            "state": cell(r, C_STATE), "dm_title": cell(r, C_DM_TITLE),
            "first": cell(r, C_FIRST) or "there",
        })
        if args.limit and len(todo) >= args.limit:
            break

    print(f"eligible leads: {len(todo)}")
    from openai import AzureOpenAI
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    if args.preview:
        # spread across personas
        byp = {"A": [], "B": [], "C": []}
        for t in todo:
            byp[persona_for(t["dm_title"])].append(t)
        picks = []
        while len(picks) < args.preview and any(byp.values()):
            for p in ("A", "B", "C"):
                if byp[p] and len(picks) < args.preview:
                    picks.append((p, byp[p].pop(0)))
        for p, t in picks:
            band, days = age_band(t["date"])
            v = get_vars(client, t)
            if not v:
                continue
            if out_of_scope(v):
                print("=" * 72)
                print(f"row {t['row']} | {t['company']} | {t['job_title']}")
                print("SKIPPED: out_of_scope — no email will be generated")
                print()
                continue
            body, etype = build_body(p, band, v, t["city"], month_name(t["date"]), t["first"])
            body, rstatus = review_body(client, body, t["company"], v["cleaned_role"])
            print("=" * 72)
            print(f"row {t['row']} | {t['company']} | DM title: {t['dm_title'] or '(none)'} "
                  f"| persona {p} | {band}d ({days}d old)")
            print(f"SUBJECT: {t['first']}, the {v['cleaned_role']} hire")
            print(f"REVIEW: {rstatus}")
            print("-" * 72)
            print(body)
            print()
        return

    lock = threading.Lock()
    pending = []
    done = 0

    def process(t):
        p = persona_for(t["dm_title"])
        band, _ = age_band(t["date"])
        v = get_vars(client, t)
        if not v:
            return None
        if out_of_scope(v):
            return [(f"{TAB}!AL{t['row']}", ["out_of_scope"]), None]
        month = month_name(t["date"])
        body, etype = build_body(p, band, v, t["city"], month, t["first"])
        body, rstatus = review_body(client, body, t["company"], v["cleaned_role"])
        row = t["row"]
        vals = [p, band, v["cleaned_role"], v["role_plural"], v["team_word"],
                etype, month, v["casual_company"], rstatus,
                v.get("casual_first", ""), v.get("casual_city", "")]
        return [(f"{TAB}!Z{row}", [body]),
                (f"{TAB}!AD{row}:AN{row}", vals)]

    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(process, chunk))
        data_upd = []
        for res in results:
            if not res:
                continue
            body_upd, vars_upd = res
            data_upd.append({"range": body_upd[0], "values": [body_upd[1]]})
            if vars_upd:
                data_upd.append({"range": vars_upd[0], "values": [vars_upd[1]]})
        if data_upd:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": data_upd}).execute()
        done += len(chunk)
        print(f"  -- batch written ({done}/{len(todo)})")
    print("Done.")


if __name__ == "__main__":
    main()
