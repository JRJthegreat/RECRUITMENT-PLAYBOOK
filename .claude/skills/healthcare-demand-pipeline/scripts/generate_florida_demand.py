"""
Phase 4 - Florida healthcare demand campaign: generate the assembled email body.

CLONED from generate_texas_demand.py, which feeds the LIVE Texas campaign and
must not be disturbed. Same client (Brent, B3 Resource Solutions), different
copy and a different sheet.

Copy is Jude's, approved 2026-08-01. FIXED except the marked slots.

  THE PROOF LINE IS A FACTUAL CLAIM ABOUT A REAL PLACEMENT (an aesthetics
  clinic, NP role, 45 days open, filled in a week, via a recruiter in Jude's
  network). "a clinic" is HARD-CODED and the geography was deliberately
  DROPPED from the Texas wording ("here in Texas") so the claim stays true in
  a Florida inbox without pretending it happened there.

  NO BENCH CLAIM. An earlier draft said the recruiter "always keeps a bench of
  pre-vetted {role_plural}". It was cut on Jude's own experience: leads reply
  "send them over", the recruiter takes the call with nobody ready, and the
  company is burned. Every claim in this copy is HISTORICAL, so nothing the
  reader is invited to do can falsify it. Do not reintroduce a present-tense
  claim about candidate availability.

PARAGRAPH SPACING IS DELIBERATE AND UNEVEN. The identity line and the proof
line are joined by a SINGLE newline while every other break is double. Uniform
one-paragraph-one-gap rhythm reads as AI-written (Jude, 2026-08-01). Do not
"tidy" this.

employer_type must be SPECIFIC. It now appears in the identity line ("I connect
{employer_type} with recruiters..."), and on Indiana/Texas it fell back to the
generic "healthcare employers" for ~1/3 of leads. A generic value would produce
exactly the bland line the personalisation was meant to remove, so those rows
are SKIPPED rather than sent.

Per lead, one GPT-4.1 JSON call extracts 4 slots from the job description:
  cleaned_role    bare noun phrase, no trailing "role"/"position"
  role_plural     plural form of the same
  employer_type   plural employer noun describing THIS employer (must be specific)
  city            the city the ROLE is in, per the POSTING

Writes: Z body, AE-AJ audit trail. AD holds the waterfall's dm_status on this
sheet and is NEVER written here.

Batch-of-10 writes; idempotent (skips rows where AE is already set).

Usage:
  python3 -W ignore generate_florida_demand.py --sheet_url "URL" --preview 20
  python3 -W ignore generate_florida_demand.py --sheet_url "URL" --limit 25
  python3 -W ignore generate_florida_demand.py --sheet_url "URL"
"""

import os
import re
import json
import argparse
import threading
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

BATCH = 10
WORKERS = 8

SUBJECT = "Sanity checking timing"

# --- Column indices (0-based). Verified against "Healthcare Texas Indeed
# --- Leads" / Outreach tab, 2026-07-22. AD = Keep Reason, DO NOT WRITE.
C_TITLE, C_DATE, C_DESC, C_COMPANY, C_SIZE = 1, 4, 9, 10, 12
C_CITY, C_STATE = 17, 18
C_DM_TITLE, C_EMAIL, C_FIRST = 20, 22, 23
C_BODY, C_STATUS = 25, 27
C_KEEP_REASON = 29                                    # AD - never written
C_ROLE, C_RPLUR, C_ETYPE, C_CFIRST, C_CCITY, C_GSTAT = 30, 31, 32, 33, 34, 35
AUDIT_HDRS = ["cleaned_role", "role_plural", "employer_type",
              "casual_first", "casual_city", "gen_status"]

# ---------------------------------------------------------------- copy ----
BODY = """Hi {first},

Is the {cleaned_role} role in {city} still open?

My name is Rood. I connect {employer_type} with recruiters who are actually known in their niche.
Recently a clinic had an NP role open for 45 days. I introduced them to one of the recruiters in my network and the role was filled in a week.

I know a recruiter who specialises in placing {role_plural}. He only works on contingent, so nothing is owed unless the hire sticks.

Before I make an intro, I just wanted to check timing. {cta}"""

# The CTA names the role only when the role is SHORT. "Is having this MRI
# technologist a priority" reads fine; "Is having this vascular ultrasound
# technologist a priority" does not, because "having this X" wants a person and
# a four-word job title stops sounding like one. Three or more words falls back
# to the plain Texas phrasing (Jude, 2026-08-01).
CTA_NAMED = "Is having this {cleaned_role} a priority right now, or is it more exploratory?"
CTA_PLAIN = "Is this hire a priority right now, or is it more exploratory?"
# NO SIGN-OFF. The Instantly sequence appends "Best,{{sendingAccountFirstName}}"
# + "Sent from my iPhone" to step 1. Adding one here signs every email twice.

# Sentences that must survive intact. Guards against a bad slot silently
# mangling the fixed copy.
REQUIRED = [
    "My name is Rood.",
    "Recently a clinic had an NP role open for 45 days.",
    "the role was filled in a week.",
    "He only works on contingent, so nothing is owed unless the hire sticks.",
    "Before I make an intro, I just wanted to check timing.",
]
FORBIDDEN = ["Best,", "Rood\n", "Sent from my iPhone"]   # sequence owns these

SYSTEM = """You extract four short slots for a cold email to a healthcare employer
that posted a job. Reply with ONLY JSON. No prose.

{"cleaned_role": "...", "role_plural": "...", "employer_type": "...", "city": "...", "casual_city": "..."}

cleaned_role
  How a person would SAY this job in conversation. A bare noun phrase.
  - NEVER include the words role, position, opening, job, vacancy, hire.
    (The email already says "the {cleaned_role} role", so "CT technologist
    role" would render as "role role".)
  - NEVER include seniority/shift/employment cruft: PRN, Full-Time, Part-Time,
    Sign-on Bonus, Hybrid, Remote, I/II/III, Senior, roman numerals, dashes,
    parentheses, location names, department codes.
  - Keep real acronyms UPPERCASE (NP, PA, RN, CT, MRI, SLP, OT, PT, LVN, CRNA,
    EMT, APRN). Everything else sentence case, not Title Case.
  - Max 5 words. Prefer how a clinician would say it.
  Examples:
    "Radiologic Technologist (CT) - PRN Nights"        -> "CT technologist"
    "Speech-Language Pathologist (SLP)"                -> "speech-language pathologist"
    "Advanced Practice Nurse Practitioner"             -> "nurse practitioner"
    "Physical Therapist II - Outpatient Ortho"         -> "orthopedic physical therapist"
    "APRN/Physician Assistant-Peri Anesthesia"         -> "APRN"

role_plural
  Plural of cleaned_role, same casing rules. "CT technologists", "NPs",
  "speech-language pathologists". Must actually be plural.

employer_type
  A PLURAL noun phrase describing what kind of employer THIS company is,
  read off the job description. It lands in: "a recruiter who helps
  {employer_type} find pre-vetted ...".
  - Max 4 words, plural, lowercase.
  - Describe the EMPLOYER, never the recruiter or the candidate.
  - Do NOT say "clinics" unless it really is a clinic. Most of this list is
    not. Use what the posting shows: "hospitals", "health systems",
    "outpatient therapy practices", "nursing and rehab facilities",
    "community health centers", "dental practices", "imaging centers",
    "home health agencies", "urgent care clinics", "school districts",
    "behavioral health practices", "surgery centers", "medical labs",
    "senior living communities", "private medical practices".
  - If the posting does not make the employer type unmistakable, return
    exactly "healthcare employers". A safe generic beats a confident guess.

city
  The city the ROLE is actually in, per the posting. The posting overrides
  any other source. Return "" if the posting does not state it clearly.
  City name only, no state.

casual_city
  How a LOCAL would say that city out loud, per the casualize-names skill.
  Use a common local nickname ONLY where one genuinely exists, otherwise
  return the city unchanged. A made-up shortening is worse than the full name
  because it reads as someone pretending to be local.
  Florida examples that are real:
    "Jacksonville"     -> "Jax"
    "St. Petersburg"   -> "St. Pete"
    "Saint Petersburg" -> "St. Pete"
    "Boca Raton"       -> "Boca"
    "West Palm Beach"  -> "West Palm"
    "Daytona Beach"    -> "Daytona"
    "Fort Lauderdale"  -> "Fort Lauderdale"   (leave it, locals say it in full)
    "Miami"            -> "Miami"
    "Orlando"          -> "Orlando"
    "Fort Walton Beach"-> "Fort Walton Beach"
  If unsure, return the city unchanged.
"""

NICKNAMES = {
    "william": "Will", "robert": "Rob", "richard": "Rich", "michael": "Mike",
    "christopher": "Chris", "matthew": "Matt", "daniel": "Dan", "david": "Dave",
    "james": "Jim", "joseph": "Joe", "thomas": "Tom", "charles": "Charlie",
    "anthony": "Tony", "steven": "Steve", "stephen": "Steve", "andrew": "Andy",
    "kenneth": "Ken", "joshua": "Josh", "timothy": "Tim", "edward": "Ed",
    "jeffrey": "Jeff", "gregory": "Greg", "benjamin": "Ben", "samuel": "Sam",
    "patricia": "Pat", "jennifer": "Jen", "elizabeth": "Liz", "katherine": "Kate",
    "kathleen": "Kathy", "margaret": "Maggie", "deborah": "Deb", "rebecca": "Becca",
    "jacqueline": "Jackie", "alexandra": "Alex", "alexander": "Alex",
    "nicholas": "Nick", "jonathan": "Jon", "zachary": "Zach", "victoria": "Vicki",
    "pamela": "Pam", "cynthia": "Cindy", "sandra": "Sandy", "theodore": "Ted",
    "raymond": "Ray", "lawrence": "Larry", "ronald": "Ron", "donald": "Don",
    "douglas": "Doug", "frederick": "Fred", "leonard": "Len", "vincent": "Vince",
}

ACRONYMS = {"NP", "PA", "RN", "CT", "MRI", "SLP", "OT", "PT", "LVN", "LPN",
            "CRNA", "EMT", "APRN", "CNA", "ICU", "ER", "OR", "CMA", "RT",
            "MLS", "MLT", "LMSW", "LCSW", "BCBA", "DPT", "NICU", "PACU"}

CRUFT = re.compile(
    r"\b(prn|per diem|full[- ]?time|part[- ]?time|contract|travel|temp|"
    r"sign[- ]?on|bonus|hybrid|remote|on[- ]?site|onsite|days?|nights?|"
    r"weekend|shift|new grad|senior|sr|junior|jr|lead|i{1,3}|iv|v)\b", re.I)
# Values that are structurally valid but say nothing. The model returns these
# when it cannot tell what the employer is, and they pass every shape check:
# two words, plural, no banned term. "I connect healthcare employers with
# recruiters" is precisely the bland line the personalisation exists to remove.
GENERIC_ETYPE = re.compile(
    r"^(healthcare|medical|health ?care|clinical)?\s*"
    r"(employers|providers|organizations|organisations|companies|businesses|"
    r"practices|facilities|clinics|groups|centers|centres|teams|offices)$", re.I)

BANNED_ETYPE = re.compile(
    r"recruit|staffing|agency|agencies|candidate|talent|client|employer of|"
    r"placement|search firm|headhunt", re.I)


def casual_first(name):
    n = (name or "").strip().split()[0] if (name or "").strip() else ""
    if not n:
        return ""
    return NICKNAMES.get(n.lower(), n if n[:1].isupper() else n.title())


def idx_to_col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def tidy_role(s, fallback):
    """Strip trailing role-nouns, cruft and punctuation. Fix casing."""
    s = (s or "").strip()
    s = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", s)          # drop (CT) / [PRN]
    s = re.sub(r"[^A-Za-z0-9\-/ ]+", " ", s)
    s = re.sub(r"\b(role|position|opening|job|vacancy|hire)s?\b", " ", s, flags=re.I)
    s = CRUFT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -/")
    if not s or len(s.split()) > 6:
        s = re.sub(r"\s+", " ", CRUFT.sub(" ", (fallback or ""))).strip(" -/")
    words = []
    for w in s.split():
        # "APRNs"/"NPs" must keep the acronym uppercase and the plural s lower
        if w.endswith(("s", "S")) and w[:-1].upper() in ACRONYMS:
            words.append(w[:-1].upper() + "s")
        elif w.upper() in ACRONYMS:
            words.append(w.upper())
        else:
            words.append(w.lower())
    return " ".join(words)[:60].strip()


def pluralize(s):
    if not s:
        return s
    head = s.split()
    last = head[-1]
    if last.upper() in ACRONYMS:
        head[-1] = last + "s"
    elif re.search(r"(s|x|z|ch|sh)$", last, re.I):
        head[-1] = last + "es"
    elif re.search(r"[^aeiou]y$", last, re.I):
        head[-1] = last[:-1] + "ies"
    else:
        head[-1] = last + "s"
    return " ".join(head)


def get_vars(client, lead):
    desc = " ".join((lead["desc"] or "").split())[:5000]
    user = (f'Company: "{lead["company"]}"\n'
            f'Job title: "{lead["job_title"]}"\n'
            f'Sheet location (may be wrong): {lead["city"]}, {lead["state"]}\n'
            f'Company size: {lead["size"] or "unknown"}\n\n'
            f'JOB POSTING:\n{desc}')
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

    notes = []

    # --- cleaned_role -----------------------------------------------------
    role = tidy_role(v.get("cleaned_role"), lead["job_title"])
    if not role:
        return None
    v["cleaned_role"] = role

    # --- role_plural ------------------------------------------------------
    rp = tidy_role(v.get("role_plural"), "")
    if not rp or rp == role or len(rp.split()) > 6:
        rp = pluralize(role)
        notes.append("plural_derived")
    v["role_plural"] = rp

    # --- employer_type ----------------------------------------------------
    et = " ".join((v.get("employer_type") or "").split()).lower().strip(" .,")
    if (not et or len(et.split()) > 4 or BANNED_ETYPE.search(et)
            or GENERIC_ETYPE.match(et) or not re.search(r"s$", et)):
        et = "healthcare employers"
        # The identity line reads "I connect {employer_type} with recruiters".
        # A generic value renders the exact bland sentence the personalisation
        # was added to remove, so the row is dropped rather than sent.
        notes.append("etype_fallback")
        return "SKIP_GENERIC_ETYPE"
    v["employer_type"] = et

    # --- city: posting wins, sheet is the fallback ------------------------
    city = " ".join((v.get("city") or "").split()).strip(" .,")
    if not city or len(city) > 30 or len(city.split()) > 3:
        city = lead["city"]
        notes.append("city_from_sheet")
    elif city.lower() != (lead["city"] or "").lower():
        notes.append(f"city_from_posting({lead['city'] or 'blank'}->{city})")
    if not city:
        return None
    v["city"] = city

    # casual_city per the casualize-names skill: a real local nickname or the
    # city unchanged. Guarded — the model must not invent a shortening, so
    # anything that is not a prefix/abbreviation of the resolved city is
    # rejected back to the full name.
    cc = " ".join((v.get("casual_city") or "").split()).strip(" .,")
    if not cc or len(cc) > len(city) + 2:
        cc = city
    else:
        a = re.sub(r"[^a-z]", "", cc.lower())
        b = re.sub(r"[^a-z]", "", city.lower())
        if a and a != b and not b.startswith(a[:4]):
            cc = city
            notes.append("casual_city_rejected")
    if cc != city:
        notes.append(f"casual_city({city}->{cc})")
    v["casual_city"] = cc

    v["casual_first"] = casual_first(lead["first"]) or "there"
    v["notes"] = ",".join(notes) or "ok"
    return v


def build_body(v):
    role_words = len([w for w in (v["cleaned_role"] or "").split() if w])
    cta = (CTA_NAMED.format(cleaned_role=v["cleaned_role"]) if role_words <= 2
           else CTA_PLAIN)
    body = BODY.format(first=v["casual_first"], cta=cta,
                       cleaned_role=v["cleaned_role"],
                       city=v["casual_city"], employer_type=v["employer_type"],
                       role_plural=v["role_plural"])
    body = body.replace("—", ", ").replace("–", "-")   # no em dashes
    return body


def verify(body):
    """Fixed copy must survive slot substitution intact."""
    if "{" in body or "}" in body:
        return "unfilled_slot"
    for s in REQUIRED:
        if s not in body:
            return "fixed_copy_broken"
    if re.search(r"\brole role\b|\bthe  \b|\bin  still\b", body):
        return "render_artifact"
    for f in FORBIDDEN:
        if f in body:
            return "body_contains_signoff"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--limit", type=int, default=0, help="0 = all eligible")
    ap.add_argument("--preview", type=int, default=0,
                    help="Render N examples, write nothing")
    args = ap.parse_args()
    TAB = args.tab

    sheet_id = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    svc = build("sheets", "v4", credentials=creds)

    rows = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB}'!A1:AZ").execute().get("values", [])
    header, data = rows[0], rows[1:]

    # Safety: AD on this sheet holds the DM adjudication pass. Never write it.
    if len(header) > C_KEEP_REASON:
        print(f"AD header is {header[C_KEEP_REASON]!r} (preserved, never written)")

    def cell(r, i):
        return r[i].strip() if i < len(r) and r[i] else ""

    if not args.preview:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sh = next(s for s in meta["sheets"] if s["properties"]["title"] == TAB)
        have = sh["properties"]["gridProperties"]["columnCount"]
        if have < C_GSTAT + 1:
            svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
                "requests": [{"appendDimension": {
                    "sheetId": sh["properties"]["sheetId"], "dimension": "COLUMNS",
                    "length": C_GSTAT + 1 - have}}]}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{TAB}'!{idx_to_col(C_ROLE)}1:{idx_to_col(C_GSTAT)}1",
            valueInputOption="RAW", body={"values": [AUDIT_HDRS]}).execute()

    todo, skipped = [], {"no_status": 0, "no_email": 0, "done": 0}
    for i, r in enumerate(data):
        # Florida's DM status lives in AD, not AB, and its vocabulary is
        # dm_only_* / dm_admin_* / amf_ceo_* — not Texas's "found_*". What
        # actually matters for sending is simpler: a named DM and a valid
        # email. Anything explicitly flagged skip_* is excluded.
        if cell(r, C_KEEP_REASON).startswith("skip_") or not cell(r, 19):
            skipped["no_status"] += 1
            continue
        if not cell(r, C_EMAIL):
            skipped["no_email"] += 1
            continue
        if cell(r, C_ROLE):
            skipped["done"] += 1
            continue
        todo.append({"row": i + 2, "company": cell(r, C_COMPANY),
                     "job_title": cell(r, C_TITLE), "desc": cell(r, C_DESC),
                     "size": cell(r, C_SIZE), "city": cell(r, C_CITY),
                     "state": cell(r, C_STATE), "dm_title": cell(r, C_DM_TITLE),
                     "first": cell(r, C_FIRST) or cell(r, 19)})
        if args.limit and len(todo) >= args.limit:
            break

    print(f"tab={TAB}  eligible={len(todo)}  "
          f"(skipped: {skipped['no_status']} status, {skipped['no_email']} email, "
          f"{skipped['done']} already generated)")
    if not todo:
        return

    from openai import AzureOpenAI
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    if args.preview:
        picks = todo[:args.preview]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            out = list(ex.map(lambda t: (t, get_vars(client, t)), picks))
        etypes = {}
        for t, v in out:
            if v == "SKIP_GENERIC_ETYPE":
                print(f"row {t['row']} | {t['company']}: skipped, "
                      f"employer_type came back generic")
                continue
            if not v:
                print(f"row {t['row']} | {t['company']}: LLM FAILED")
                continue
            body = build_body(v)
            err = verify(body)
            etypes[v["employer_type"]] = etypes.get(v["employer_type"], 0) + 1
            print("=" * 74)
            print(f"row {t['row']} | {t['company']} | size {t['size'] or '?'} "
                  f"| DM {t['dm_title'] or '(none)'}")
            print(f"  raw title : {t['job_title']}")
            print(f"  slots     : role={v['cleaned_role']!r} plural={v['role_plural']!r}")
            print(f"              etype={v['employer_type']!r} city={v['city']!r}")
            print(f"  notes     : {v['notes']}{'  ** ' + err if err else ''}")
            print(f"SUBJECT: {SUBJECT}")
            print("-" * 74)
            print(body)
            print()
        print("=" * 74)
        print("employer_type spread:")
        for k, n in sorted(etypes.items(), key=lambda x: -x[1]):
            print(f"  {n:>3}  {k}")
        return

    lock = threading.Lock()
    done = 0

    def process(t):
        v = get_vars(client, t)
        if v == "SKIP_GENERIC_ETYPE":
            with lock:
                print(f"  [-] row {t['row']} {t['company']}: employer_type generic "
                      f"- SKIPPED (would read 'I connect healthcare employers')")
            return None
        if not v:
            return None
        body = build_body(v)
        err = verify(body)
        if err:
            with lock:
                print(f"  [!] row {t['row']} {t['company']}: {err} - SKIPPED")
            return None
        return (t["row"], body,
                [v["cleaned_role"], v["role_plural"], v["employer_type"],
                 v["casual_first"], v["casual_city"], v["notes"]])

    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = [r for r in ex.map(process, chunk) if r]
        upd = []
        for row, body, vals in results:
            upd.append({"range": f"'{TAB}'!{idx_to_col(C_BODY)}{row}",
                        "values": [[body]]})
            upd.append({"range": (f"'{TAB}'!{idx_to_col(C_ROLE)}{row}:"
                                  f"{idx_to_col(C_GSTAT)}{row}"),
                        "values": [vals]})
        if upd:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": upd}).execute()
        done += len(chunk)
        print(f"  -- batch written ({done}/{len(todo)})")
    print("Done.")


if __name__ == "__main__":
    main()
