"""
Decision-maker waterfall: NPPES filer first, decision-maker endpoint only when
the filer does not qualify.

Flow (Jude, 2026-08-04):

  1. Read the NPPES filer's name + title.
  2. LLM GATE: is this person the owner/decision maker?
       MATCH    -> /find on THEIR name. Never call /decision-makers.
                   Miss -> AMF person on the same name (2nd database, same
                   correct human). Only if BOTH miss does the row fall through.
       NO MATCH -> /decision-makers, LLM ranks everyone returned, /find the
                   winner, walk down the ranking on a miss.

The gate is an LLM, not a keyword list, because owners title themselves
inconsistently: CEO, Owner, Administrator, Admin, Managing Member, or simply
their clinical credential. A dermatologist who personally filed a new
dermatology practice IS the owner; no regex encodes that reliably.

Priority ladder for the ranker: owner/CEO > VP Operations/COO > HR/talent >
back office (last resort only, never when something above exists).

Only `valid` emails are written, and a name is never written without one.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/find_dm_waterfall.py \
      --sheet_url URL --target 400 [--workers 8] [--limit N] [--dry_run]
"""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

PM_KEY = os.getenv("PURPLE_MAGIC_KEY")
PM_BASE = "https://api.connector-os.com/api/email/v2"
AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON = "https://api.anymailfinder.com/v5.1/find-email/person"
AZ_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZ_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZ_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
# GPT-5.1, not the fast tier: both calls are judgement about who holds hiring
# authority, and a wrong call here silently emails the wrong human. Note the
# newer model rejects `temperature` and `max_tokens` — it wants
# `max_completion_tokens`, and it spends some of that budget on reasoning, so
# the limits are set well above the size of the JSON we want back.
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT")

TAB = "Leads"
# commercial-pool sheet layout
C_FTYPE, C_STATUS_LEAD, C_OWNER_SITES = 1, 4, 7
C_COMPANY, C_WEBSITE = 10, 11
C_CITY, C_STATE = 17, 18
C_DM_NAME, C_DM_TITLE, C_LINKEDIN = 19, 20, 21
C_EMAIL, C_FIRST, C_LAST = 22, 23, 24
C_AO_NAME, C_AO_TITLE, C_EMAIL_STATUS = 28, 29, 31
C_SOURCE = 34            # AI: reused as the audit column (which lane won)

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
             "icloud.com", "protonmail.com", "live.com", "msn.com", "comcast.net"}

# THE FILER IS THE TARGET. This is deterministic on purpose — an LLM gate was
# tried and rejected COOs, Office Managers and Business Managers as "not the
# owner", which is wrong twice over.
#
# The reasoning (Jude, 2026-08-04): CMS defines the Authorized Official as a
# person legally empowered to BIND the organization — partner, board chair,
# officer, or someone holding delegated authority. You cannot submit that
# filing as an ordinary employee. So the act of filing IS the evidence of
# authority, and second-guessing it from the title text is backwards. An
# "Office Manager" who personally registered the entity runs the place.
#
# Only finance chiefs and outside agents are skipped, and even they are tried
# as a last resort when /decision-makers returns nobody (which it does ~83% of
# the time on small practices) — a CFO at the right company beats an empty row.
NOT_TARGET = re.compile(
    # THE TEST (Jude): can this person decide to PAY a recruitment firm to
    # staff the facility? Support functions never can. One comprehensive list,
    # not per-incident patches. Filers with clinical/admin/exec titles are
    # trusted (a dentist-filer is the owner); filers in a support FUNCTION are
    # delegated paperwork staff at a bigger org.
    r"\b(CFO|CHIEF FINANC\w*|FINANC\w*|CONTROLLER|TREASURER|ACCOUNT\w*|PAYROLL"
    r"|BILLING|REVENUE CYCLE|RCM|PATIENT ACCOUNTS|CODER|CPC|CODING"
    r"|CREDENTIAL\w*|ENROLLMENT|COMPLIANCE|PRIVACY|RISK|AUDIT\w*"
    r"|ATTORNEY|PARALEGAL|ESQ|GENERAL COUNSEL|LEGAL\w*"
    r"|MANAGED CARE|CONTRACTING|PAYOR|PAYER|PROVIDER RELATIONS"
    r"|IT|I\.T\.|INFORMATION|TECHNOLOGY|SYSTEMS|DATA|SOFTWARE|WEB|DIGITAL|CYBER"
    r"|MARKETING|SALES|COMMUNICATIONS|PUBLIC RELATIONS|MEDIA|BRAND|GROWTH"
    r"|EXTERNAL AFFAIRS|DEVELOPMENT|FOUNDATION|FUNDRAIS\w*|GRANTS?"
    r"|FRONT DESK|FRONT OFFICE|RECEPTION\w*|SCHEDUL\w*|INTAKE|REFERRAL|RECORDS"
    r"|FACILITIES|MAINTENANCE|HOUSEKEEPING|DIETARY|TRANSPORT\w*|SUPPLY|PURCHAS\w*|PROCUREMENT"
    r"|CONSULTANT|IMPACT OFFICER|SCIENTIFIC|ASSISTANT|SECRETARY|DEPUTY|OFFICE OF"
    r"|INTERN|STUDENT|VOLUNTEER)\b|^\s*ADMINISTRATIVE\s*$", re.I)

# Applies to EVERY contact written to the sheet, not just the filer — the
# earlier gap was vetoing only the filer while the ranked lane wrote whatever
# Purple Magic's array offered (Tax Collector, Senior Dotnet Developer,
# Marketing Assistant all got through). Jude's rule 2026-08-04: CFO / legal /
# billing-RCM are never targets; nor are roles with no hiring authority.
NEVER_WRITE = re.compile(
    r"\b(DEVELOPER|ENGINEER|MARKETING|CLERICAL|OFFICE SUPPORT|RECEPTIONIST"
    r"|CASE MANAGER|ACTIVITY DIRECTOR|TAX COLLECTOR|SUPERINTENDENT"
    r"|SCIENTIFIC OFFICER|IMPACT OFFICER|COMPLIANCE|SOCIAL WORKER"
    r"|STUDENT|INTERN|VOLUNTEER)\b", re.I)


# POSITIVE GATE for anyone found via /decision-makers (2026-08-04, after the
# manual review of all 421 contacts). A blocklist cannot enumerate the world —
# "Podcast Host", "Tax Collector" and "Executive Assistant to CEO" all sailed
# past one. So ranked candidates must AFFIRMATIVELY match the ladder, and
# disqualifiers are checked FIRST so "Assistant to CEO" can never ride the
# word CEO through the gate. Unknown titles now FAIL by default.
DISQUALIFIER = re.compile(
    r"\b(ASSISTANT|SECRETARY|DEPUTY|AIDE|OFFICE OF|INTERN|STUDENT|VOLUNTEER"
    r"|IT\b|IS\b|INFORMATION (TECH|SECURITY|SYSTEMS)|TECHNOLOGY|INFRASTRUCTURE|CTO|SECURITY"
    r"|MARKETING|SALES|COMMUNICATIONS|PUBLIC RELATIONS|EXTERNAL AFFAIRS"
    r"|DEVELOPER|ENGINEER|PODCAST|PUBLISHER|DEVELOPMENT|FOUNDATION"
    r"|CFO|FINANC|CONTROLLER|TREASURER|ACCOUNT|BILLING|REVENUE|RCM"
    r"|CREDENTIAL|ENROLLMENT|COMPLIANCE|LEGAL|COUNSEL|ATTORNEY|PARALEGAL"
    r"|CONTRACTING|PROVIDER RELATIONS|MANAGED CARE|CASE MANAGER|COORDINATOR"
    r"|SPECIALIST|ANALYST|CLERICAL|RECEPTIONIST|SUPPORT|TEACHER|PROFESSOR"
    r"|DEAN|SUPERINTENDENT|LAB DIRECTOR|SCIENTIFIC|IMPACT OFFICER"
    r"|CONSULTANT|SUPERVISOR)\b", re.I)
LADDER = re.compile(
    r"\b(OWNER|CO-?OWNER|PROPRIETOR|CEO|CHIEF EXECUTIVE|FOUNDER|CO-?FOUNDER"
    r"|PRESIDENT|PRINCIPAL|PARTNER|MANAGING (MEMBER|PARTNER|DIRECTOR)"
    r"|EXECUTIVE DIRECTOR|COO|CHIEF OPERATING|CHIEF ADMINISTRATIVE"
    r"|CHIEF CLINICAL|CHIEF MEDICAL|VP|VICE PRESIDENT|GENERAL MANAGER"
    r"|ADMINISTRATOR|ADMIN|OFFICE MANAGER|BUSINESS MANAGER|PRACTICE MANAGER"
    r"|OPERATIONS (MANAGER|DIRECTOR)|DIRECTOR OF OPERATIONS|AGENCY DIRECTOR"
    r"|CLINIC(AL)? DIRECTOR|MEDICAL DIRECTOR|PROGRAM DIRECTOR|DIRECTOR"
    r"|CHRO|HUMAN RESOURCES|\bHR\b|PEOPLE|TALENT|RECRUIT)\w*", re.I)


# A title that BEGINS with an executive role wins even if a legal/finance
# tail follows ("COO and General Counsel" — the ops half wins, Jude). Anchored
# at the start so "Assistant to CEO" can never exploit it.
EXEC_LEAD = re.compile(
    r"^\s*(CO-?)?(COO|CHIEF OPERATING|OWNER|CEO|CHIEF EXECUTIVE|PRESIDENT"
    r"|FOUNDER|PRINCIPAL|MANAGING (MEMBER|PARTNER|DIRECTOR)|EXECUTIVE DIRECTOR)\b",
    re.I)


def title_ok(title):
    """Ladder membership required; disqualifiers always win — except a title
    that literally leads with the executive role."""
    t = title or ""
    if EXEC_LEAD.match(t):
        return True
    if DISQUALIFIER.search(t):
        return False
    return bool(LADDER.search(t))


def title_banned(title):
    """Filer-only check (the filer is trusted by default per Jude's rule —
    the filing itself proves authority — minus finance/legal/outside agents
    and obvious proxies)."""
    t = title or ""
    if re.search(r"\b(ASSISTANT|SECRETARY|DEPUTY|OFFICE OF)\b", t, re.I):
        return True
    return bool(NOT_TARGET.search(t))

RANK_SYSTEM = """CONTEXT: We are a business-development connector. We introduce \
healthcare organizations that are opening or expanding to specialist RECRUITMENT \
FIRMS that can staff them (nurses, NPs, PAs, therapists, clinical and support \
staff). We are about to send ONE cold email to ONE person at this organization. \
The right person is whoever has the BUYING POWER to decide "yes, let's engage a \
recruitment vendor" — the person who owns the budget and feels the staffing pain. \
Typically that is the CEO or owner.

You will see EVERY person our data provider returned for this organization's \
domain. Rank ALL of them, best first, by vendor-decision buying power:

1. Owner, CEO, Chief Executive, Founder, President, Managing Partner, Principal,
   Executive Director — the default right answer
2. COO, Chief Operating Officer, VP of Operations, Director of Operations,
   Administrator, General Manager
3. HR / People / Talent leadership (CHRO, VP People, Director of Talent
   Acquisition, Director of Recruiting)
4. Back office (finance, credentialing, admin support) — rank these LAST, and
   only include them if nothing above exists at all

EXCLUDE from the ranking entirely (do not even rank them): assistants and
secretaries of any kind, IT/engineering/security, marketing/sales/PR/
communications, fundraising/development, clinical staff with no management
scope, coordinators/specialists/analysts, board members with no operating
role, and anyone whose connection to hiring decisions is not plausible.

Weigh the organization's size and situation (given below): at a small practice
an Administrator runs the place; at a large health system the same title is
middle management and you should prefer the true executives.

Return JSON only:
{"ranking": [<0-based indices, best first, ONLY people worth contacting>],
 "why": "<one line on the top choice>"}
An empty ranking [] means nobody in the list should be contacted."""


def col_letter(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def norm_domain(w):
    w = (w or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    h = urlparse(w).netloc or ""
    return h[4:] if h.startswith("www.") else h


def llm(system, user, budget=400):
    try:
        r = requests.post(
            f"{AZ_ENDPOINT}/openai/deployments/{AZ_MODEL}/chat/completions",
            params={"api-version": AZ_VERSION},
            headers={"api-key": AZ_KEY, "Content-Type": "application/json"},
            json={"messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "max_completion_tokens": budget,
                  "response_format": {"type": "json_object"}},
            timeout=120)
        if r.status_code != 200:
            return None
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return None


# Persistent cache for Purple Magic responses (Jude, 2026-08-04: "save those
# arrays somewhere — I don't want you hitting the decision-maker endpoint over
# and over making the same mistake"). Keyed by endpoint+params; re-ranking
# after a logic fix costs zero API calls. Errors are never cached.
import threading
CACHE_PATH = os.path.join(SCRIPT_DIR, "..", "data", "pm_cache.json")
_cache_lock = threading.Lock()
try:
    with open(CACHE_PATH) as _f:
        PM_CACHE = json.load(_f)
except Exception:
    PM_CACHE = {}


def _cache_save():
    with _cache_lock:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(PM_CACHE, f)
        os.replace(tmp, CACHE_PATH)


def pm(endpoint, body):
    key = endpoint + "|" + json.dumps(body, sort_keys=True)
    with _cache_lock:
        if key in PM_CACHE:
            return PM_CACHE[key], ""
    try:
        r = requests.post(PM_BASE + endpoint,
                          headers={"Authorization": f"Bearer {PM_KEY}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=120)
    except requests.RequestException as e:
        return {}, f"error:{type(e).__name__}"
    if r.status_code == 429:
        return {}, "rate_limited"
    if r.status_code != 200:
        return {}, f"error:http_{r.status_code}"
    data = r.json() or {}
    with _cache_lock:
        PM_CACHE[key] = data
        if len(PM_CACHE) % 25 == 0:
            pass  # periodic save happens below without holding the lock twice
    if len(PM_CACHE) % 25 == 0:
        _cache_save()
    return data, ""


def amf_person(full_name, domain):
    if not AMF_KEY:
        return None
    try:
        r = requests.post(AMF_PERSON,
                          headers={"Authorization": AMF_KEY,
                                   "Content-Type": "application/json"},
                          json={"full_name": full_name, "domain": domain},
                          timeout=120)
        if r.status_code != 200:
            return None
        d = r.json() or {}
        if d.get("email_status") == "valid":
            return d.get("valid_email") or d.get("email")
    except Exception:
        pass
    return None


def acceptable(email, domain):
    """Valid + not a free mailbox. Domain mismatch is ALLOWED: health systems
    legitimately answer at the parent domain, and those are good leads."""
    if not email:
        return None
    d = email.split("@")[-1].lower()
    if d in FREE_MAIL:
        return None
    return email


def get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--target", type=int, default=400)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:AZ").execute().get("values", [])

    def cell(row, i):
        return row[i].strip() if len(row) > i and row[i] else ""

    have = sum(1 for r in values if cell(r, C_EMAIL))
    todo = [(n, r, norm_domain(cell(r, C_WEBSITE)))
            for n, r in enumerate(values, start=2)
            if cell(r, C_WEBSITE) and not cell(r, C_EMAIL) and not cell(r, C_EMAIL_STATUS)]
    if args.limit:
        todo = todo[:args.limit]

    print(f"[dm] {have} verified already | target {args.target} | "
          f"{len(todo)} rows with a domain and no attempt"
          f"{' | DRY RUN' if args.dry_run else ''}")
    if args.dry_run:
        for n, r, d in todo[:10]:
            print(f"   row{n:5d} {cell(r,C_COMPANY)[:32]:32s} {d[:24]:24s} "
                  f"| filed by {cell(r,C_AO_NAME)[:20]} ({cell(r,C_AO_TITLE)[:20]})")
        return

    def resolve(item):
        n, row, dom = item
        company = cell(row, C_COMPANY)
        ctx = (f'Organization: {company}\nFacility type: {cell(row, C_FTYPE)}\n'
               f'Status: {cell(row, C_STATUS_LEAD)}\n'
               f'Sites this owner registered: {cell(row, C_OWNER_SITES) or "1"}')
        ao_name, ao_title = cell(row, C_AO_NAME), cell(row, C_AO_TITLE)

        # ---- STEP 1: the filer, who is the target unless finance/outside ----
        have_name = bool(ao_name) and len(ao_name.split()) >= 2
        filer_ok = have_name and not title_banned(ao_title)
        if filer_ok:
            parts = ao_name.split()
            d, err = pm("/find", {"firstName": parts[0], "lastName": parts[-1],
                                  "domain": dom})
            if err == "rate_limited":
                return n, None, "rate_limited"
            email = acceptable(d.get("email"), dom) if d.get("status") == "valid" else None
            if email:
                return n, (ao_name, ao_title or "Owner", email, "", "filer:pm"), ""
            # same human, second database — before ever downgrading
            email = acceptable(amf_person(ao_name, dom), dom)
            if email:
                return n, (ao_name, ao_title or "Owner", email, "", "filer:amf"), ""

        # ---- STEP 2: decision-maker endpoint, LLM-ranked -----------------
        d, err = pm("/decision-makers", {"domain": dom})
        if err == "rate_limited":
            return n, None, "rate_limited"
        cands = []
        best = (d or {}).get("best") or {}
        if best.get("fullName"):
            cands.append(best)
        for o in (d or {}).get("others") or []:
            if o.get("fullName") and o.get("fullName") != best.get("fullName"):
                cands.append(o)
        if not cands:
            # Nobody from /decision-makers and the filer was finance/legal/
            # billing. Jude 2026-08-04: those are NEVER targets, not even as a
            # last resort — an empty row beats a CFO. Leave it blank.
            return n, None, "no_candidates"

        listing = "\n".join(
            f'{i}. {c.get("fullName")} — {c.get("title") or "(no title)"}'
            f'{" | " + c["linkedIn"] if c.get("linkedIn") else ""}'
            f' [seniority {c.get("seniorityScore", "?")}, via {c.get("via", "?")}]'
            for i, c in enumerate(cands))
        pick = llm(RANK_SYSTEM,
                   f"{ctx}\n\nEvery candidate found at this organization:\n{listing}\n\n"
                   "Rank everyone worth contacting, best first.", 800)
        order = []
        if pick and isinstance(pick.get("ranking"), list):
            order = [i for i in pick["ranking"]
                     if isinstance(i, int) and 0 <= i < len(cands)]
            if not order:
                return n, None, "llm_rejected_all"
        if not order:
            # The LLM call itself failed (timeout / bad JSON). Jude's rule:
            # NO contact beats a bad contact — nobody judged these people, so
            # nobody gets written. Status marks the row for a cheap retry.
            return n, None, "llm_error_retry"

        # Walk the LLM's FULL ranking (not a slice) until an email verifies.
        # title_ok stays as a code-level backstop on every single write.
        for idx in order:
            c = cands[idx]
            if not title_ok(c.get("title")):
                continue                 # POSITIVE gate — unknown titles fail
            fn, ln = c.get("firstName"), c.get("lastName")
            if not (fn and ln):
                parts = (c.get("fullName") or "").split()
                fn, ln = (parts[0], parts[-1]) if len(parts) >= 2 else (None, None)
            if not (fn and ln):
                continue
            d2, err2 = pm("/find", {"firstName": fn, "lastName": ln, "domain": dom})
            if err2 == "rate_limited":
                return n, None, "rate_limited"
            email = acceptable(d2.get("email"), dom) if d2.get("status") == "valid" else None
            lane = "ranked" if idx == order[0] else "ranked_fallback"
            if not email:
                # Jude 2026-08-04: AMF as a parallel attempt on the SAME judged
                # person — a fresh name+domain is where AMF earns its 26%, and
                # a miss costs zero credits. (Only as a domain-level rescue on
                # PM's failures did it measure ~0%.)
                email = acceptable(amf_person(f"{fn} {ln}", dom), dom)
                lane += ":amf"
            if email:
                return n, (c.get("fullName"), c.get("title") or "", email,
                           c.get("linkedIn") or "", lane), ""
        return n, None, "no_valid_email"

    updates, found, attempted, stop = [], have, 0, False
    lanes, misses = {}, {}
    for i in range(0, len(todo), args.workers * 4):
        if stop or found >= args.target:
            break
        for n, win, reason in ThreadPoolExecutor(max_workers=args.workers).map(
                resolve, todo[i:i + args.workers * 4]):
            if reason == "rate_limited":
                print("\n[dm] 429 — stopping"); stop = True; continue
            attempted += 1
            if win:
                name, title, email, li, lane = win
                found += 1
                lanes[lane] = lanes.get(lane, 0) + 1
                p = name.split()
                for idx, val in ((C_DM_NAME, name), (C_DM_TITLE, title),
                                 (C_LINKEDIN, li), (C_EMAIL, email),
                                 (C_FIRST, p[0]), (C_LAST, p[-1] if len(p) > 1 else ""),
                                 (C_EMAIL_STATUS, "valid"), (C_SOURCE, lane)):
                    updates.append({"range": f"'{TAB}'!{col_letter(idx)}{n}",
                                    "values": [[val]]})
            else:
                misses[reason] = misses.get(reason, 0) + 1
                updates.append({"range": f"'{TAB}'!{col_letter(C_EMAIL_STATUS)}{n}",
                                "values": [[reason]]})
        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": updates}).execute()
            updates = []
        print(f"  attempted {attempted}/{len(todo)} | verified {found}/{args.target}", end="\r")

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
    _cache_save()
    print(f"\n[dm] {attempted} attempted -> {found - have} new | {found} verified total "
          f"| pm cache: {len(PM_CACHE)} responses stored")
    if lanes:
        print("  won by: " + ", ".join(f"{k} {v}" for k, v in sorted(lanes.items(), key=lambda x: -x[1])))
    if misses:
        print("  missed: " + ", ".join(f"{k} {v}" for k, v in sorted(misses.items(), key=lambda x: -x[1])[:6]))


if __name__ == "__main__":
    main()
