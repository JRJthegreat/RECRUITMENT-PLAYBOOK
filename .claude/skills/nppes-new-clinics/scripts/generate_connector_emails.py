"""
Phase 4 — generate connector email bodies for the commercial-pool campaign.

Jude's framework (2026-08-04, verbatim — only the marked {variables} change):

  VARIANT A (NEW PRACTICE):
    Hi {first}
    Saw you're getting {company} up and running in {city}. Congrats.
    I have recruiters who specialize in staffing {ptype}, and a couple are
    taking on 2-3 {unit} right now. Figured this could be relevant.
    Are you planning to hire staff in the next 60 to 90 days?

  VARIANT B (NEW LOCATION / HEALTH-SYSTEM SITE):
    Hi {first}
    Saw {company} is opening a new location in {city}.
    I have recruiters who help {gtype} staff new sites, and a couple have
    bandwidth right now. Figured this could be relevant given the expansion
    in {city}.
    Are you thinking about hiring for the new location in the next 60 to 90 days?

{ptype}/{gtype} come from a fixed NUCC-code map (deterministic, no LLM — the
copy is the same every time for the same practice type). Generic facility
codes fall back to Jude's original generic wording rather than a bland
made-up phrase. Casualization embedded per the standing rules: nickname
first names, strip legal suffixes from companies, city nicknames.

Templates are plain text, no links, no em dashes. Bodies land in col Z.
Idempotent: rows with Z filled are skipped. Batch-of-10 writes.

  --preview N   render N bodies to stdout, write NOTHING (approval gate)
  --limit N     cap rows written
"""
import argparse
import json
import re
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "..", "token.json")
TAB = "Leads"
C_FTYPE, C_TAX, C_STATUS_LEAD = 1, 3, 4
C_COMPANY, C_CITY, C_STATE = 10, 17, 18
C_EMAIL, C_FIRST, C_BODY = 22, 23, 25

# ---- casualization (canonical rules: casualize-names skill) ----
NICKNAMES = {
    "WILLIAM": "Will", "ROBERT": "Rob", "MICHAEL": "Mike", "CHRISTOPHER": "Chris",
    "MATTHEW": "Matt", "JOSHUA": "Josh", "DANIEL": "Dan", "DAVID": "Dave",
    "JAMES": "Jim", "JOSEPH": "Joe", "THOMAS": "Tom", "CHARLES": "Charlie",
    "ANTHONY": "Tony", "STEVEN": "Steve", "STEPHEN": "Steve", "ANDREW": "Andy",
    "KENNETH": "Ken", "GREGORY": "Greg", "JONATHAN": "Jon", "TIMOTHY": "Tim",
    "BENJAMIN": "Ben", "SAMUEL": "Sam", "ALEXANDER": "Alex", "NICHOLAS": "Nick",
    "EDWARD": "Ed", "DOUGLAS": "Doug", "ZACHARY": "Zach", "JEFFREY": "Jeff",
    "KATHERINE": "Kate", "ELIZABETH": "Liz", "JENNIFER": "Jen", "REBECCA": "Becca",
    "STEPHANIE": "Steph", "PATRICIA": "Pat", "MARGARET": "Maggie", "VICTORIA": "Vicki",
    "JACQUELINE": "Jackie", "KIMBERLY": "Kim", "PAMELA": "Pam", "DEBORAH": "Deb",
    "CYNTHIA": "Cindy", "SANDRA": "Sandy", "RONALD": "Ron", "RAYMOND": "Ray",
    "LAWRENCE": "Larry", "GERALD": "Jerry", "FREDERICK": "Fred", "RICHARD": "Rich",
}
CITY_NICKS = {
    "INDIANAPOLIS": "Indy", "PHILADELPHIA": "Philly", "SAN FRANCISCO": "SF",
    "LOS ANGELES": "LA", "LAS VEGAS": "Vegas", "NEW YORK": "New York",
    "OKLAHOMA CITY": "OKC", "SALT LAKE CITY": "SLC", "FORT LAUDERDALE": "Fort Lauderdale",
}
LEGAL_RE = re.compile(
    r"[,\s]+(L\.?L\.?C\.?|P\.?L\.?L\.?C\.?|INC\.?|CORP\.?|CORPORATION|P\.?C\.?"
    r"|P\.?A\.?|L\.?L\.?P\.?|LTD\.?|PLC|CO\.?|COMPANY|INCORPORATED|NFP|PLLP|LLLP)$",
    re.I)
# "X, A Department of Y" / "X, a division of Y" / "X DBA Y" — keep the local
# identity, drop the corporate tail (HCA legal names are a full sentence)
TAIL_RE = re.compile(r"\s*,?\s+(A DEPARTMENT OF|A DIVISION OF|AN AFFILIATE OF"
                     r"|A SERVICE OF|D/?B/?A)\s+.*$", re.I)
SMALL = {"of", "and", "the", "at", "for", "in", "on"}
# short all-caps tokens that really are acronyms and must stay uppercase
ACRONYMS = {"ABA", "ENT", "OB", "GYN", "OBGYN", "ER", "PT", "OT", "SLP", "MRI",
            "FQHC", "HCA", "IV", "TMS", "ABC", "USA", "DFW", "NW", "SW", "SE",
            "NE", "TLC", "VIP", "CNY", "NY", "LA", "SF", "UC"}


def casual_first(name):
    n = (name or "").strip()
    return NICKNAMES.get(n.upper(), n.title() if n.isupper() or n.islower() else n)


# short all-caps tokens that are real WORDS, not acronyms
COMMON_SHORT = {"two", "one", "six", "ten", "new", "old", "big", "sun", "sky",
                "joy", "day", "all", "our", "you", "her", "his", "its", "own",
                "top", "care", "home", "life", "hope", "help", "hand", "west",
                "east", "gold", "blue", "oak", "elm", "bee", "fox", "j", "a"}


def _fix_word(w, i):
    lw = w.lower()
    if lw in SMALL and i > 0:
        return lw
    if w.upper() in ACRONYMS:
        return w.upper()
    # a 2-3 letter all-caps token that is not a common word is an acronym
    # (ECU, RCH, DFW...) — keep it uppercase rather than mangling to "Ecu"
    if w.isupper() and 2 <= len(w) <= 3 and lw not in COMMON_SHORT:
        return w.upper()
    # all-consonant caps of any length are acronyms too (RCH, HCPS)
    if w.isupper() and len(w) <= 5 and not any(ch in "AEIOU" for ch in w):
        return w.upper()
    if w.isupper() or w.islower():
        # PRIMARYMD -> PrimaryMD; otherwise plain capitalize
        if len(w) > 4 and w.upper().endswith("MD"):
            return w[:-2].capitalize() + "MD"
        if "-" in w:              # HEALTH-CINCO -> Health-Cinco
            return "-".join(seg.capitalize() for seg in w.split("-"))
        return w.capitalize()
    return w                      # already mixed case, trust it


def casual_company(name):
    n = TAIL_RE.sub("", (name or "").strip()).strip(" ,.")
    for _ in range(3):
        n = LEGAL_RE.sub("", n).strip(" ,.")
    return " ".join(_fix_word(w, i) for i, w in enumerate(n.split()))


def casual_city(city):
    c = (city or "").strip()
    return CITY_NICKS.get(c.upper(), c.title())


# ---- NUCC code -> practice-type phrases ----
# (ptype fits "staffing {ptype}"; gtype fits "help {gtype} staff new sites";
#  unit fits "taking on 2-3 {unit} right now")
TYPE_MAP = [
    ("261QM08",     "mental health clinics",            "behavioral health groups",   "clinics"),
    ("2084P",       "psychiatry practices",             "behavioral health groups",   "practices"),
    ("323P",        "psychiatric residential programs", "behavioral health groups",   "programs"),
    ("320800000X",  "residential mental health programs", "behavioral health groups", "programs"),
    ("322D",        "youth residential treatment programs", "behavioral health groups", "programs"),
    ("3245",        "addiction treatment centers",      "treatment center groups",    "centers"),
    ("207Q",        "family medicine practices",        "primary care groups",        "practices"),
    ("207R",        "internal medicine practices",      "primary care groups",        "practices"),
    ("208D",        "medical practices",                "medical groups",             "practices"),
    ("2080",        "pediatric practices",              "pediatric groups",           "practices"),
    ("207N",        "dermatology practices",            "dermatology groups",         "practices"),
    ("207V",        "OB/GYN practices",                 "women's health groups",      "practices"),
    ("261QF0400X",  "community health centers",         "community health organizations", "centers"),
    ("261QC1500X",  "community health clinics",         "community health organizations", "clinics"),
    ("261QR1300X",  "rural health clinics",             "rural health organizations", "clinics"),
    ("261QM1300X",  "multi-specialty clinics",          "multi-specialty groups",     "clinics"),
    ("261QM2500X",  "specialty clinics",                "specialty groups",           "clinics"),
    ("261QP2300X",  "primary care clinics",             "primary care groups",        "clinics"),
    ("261QA1903X",  "surgery centers",                  "surgical groups",            "centers"),
    ("261QP2000X",  "physical therapy clinics",         "therapy groups",             "clinics"),
    ("261QR0200X",  "imaging centers",                  "imaging groups",             "centers"),
    ("261QU0200X",  "urgent care clinics",              "urgent care groups",         "clinics"),
    ("261QE0002X",  "emergency care clinics",           "emergency care groups",      "clinics"),
    ("261QX0100X",  "occupational medicine clinics",    "occupational health groups", "clinics"),
    ("1223",        "dental practices",                 "dental groups",              "practices"),
    ("122300000X",  "dental practices",                 "dental groups",              "practices"),
    ("124Q",        "dental practices",                 "dental groups",              "practices"),
    ("251E",        "home health agencies",             "home health groups",         "agencies"),
    ("253Z",        "home care agencies",               "home care groups",           "agencies"),
    ("374U",        "home care agencies",               "home care groups",           "agencies"),
    ("376J",        "home care agencies",               "home care groups",           "agencies"),
    ("3747",        "home care agencies",               "home care groups",           "agencies"),
    ("251J",        "nursing care agencies",            "home care groups",           "agencies"),
    ("310400000X",  "assisted living communities",      "senior living operators",    "communities"),
    ("314000000X",  "skilled nursing facilities",       "senior care operators",      "facilities"),
    ("282N",        "hospitals",                        "health systems",             "facilities"),
]


def phrases(code):
    for prefix, p, g, u in TYPE_MAP:
        if code.startswith(prefix):
            return p, g, u
    return None, None, None      # generic codes -> Jude's original generic wording


VARIANT_A = """Hi {first}

Saw you're getting {company} up and running in {city}. Congrats.

I have recruiters who specialize in staffing {ptype}, and a couple are taking on 2-3 {unit} right now. Figured this could be relevant.

Are you planning to hire staff in the next 60 to 90 days?"""

VARIANT_A_GENERIC = VARIANT_A.replace("staffing {ptype}", "staffing new practices").replace("2-3 {unit}", "2-3 clinics")

VARIANT_B = """Hi {first}

Saw {company} registered a new location in {city}.

I have recruiters who help {gtype} staff new sites, and a couple have bandwidth right now. Figured this could be relevant given the expansion.

Are you thinking about hiring for the new location in the next 60 to 90 days?"""

VARIANT_B_GENERIC = VARIANT_B.replace("help {gtype} staff", "help growing groups staff")

# Variant C (Jude, 2026-08-04): one DM covering MULTIPLE distinct new
# addresses gets one email that acknowledges all of them. "registered", not
# "opening" — NPPES proves the registration, not a ribbon-cutting (Sarasota
# check: some subpart filings formalize sites that already operate).
VARIANT_C = """Hi {first}

Saw {company} registered several new locations{where}.

I have recruiters who help {gtype} staff new sites, and a couple have bandwidth right now. Figured this could be relevant with multiple openings at once.

Are you thinking about hiring for the new locations in the next 60 to 90 days?"""

VARIANT_C_GENERIC = VARIANT_C.replace("help {gtype} staff", "help growing groups staff")

# Sites are distinct by STREET ADDRESS, not by row — suite numbers in one
# building collapse to one site (Marietta registered 30+ suite-level NPIs).
UNIT_RE = re.compile(r"\s+(STE|SUITE|UNIT|APT|RM|ROOM|BLDG|BUILDING|FL|FLOOR|#).*$", re.I)
C_PARENT, C_ADDR, C_ZIP = 9, 32, 33

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "DC",
}

# Hand-set display brands for multi-site groups where the sheet holds legal
# names, site-suffixed names, or street addresses (reviewed 2026-08-04).
BRAND_BY_EMAIL = {
    "jbullman@mhsystem.org": "Memorial Health System",
    "rspencer@kintegra.org": "Kintegra Health",
    "michael.holmes@ufhealth.org": "UF Health",
    "david-verinder@smh.com": "Sarasota Memorial",
    "aduke@sandhillsmedical.org": "Sandhills Medical",
    "mia.jones@agapefamilyhealth.org": "Agape Family Health",
    "kenley.kelly@odhc.org": "Open Door Health Center",
    "kc.donahey@hcahealthcare.com": "HCA Florida Fort Walton-Destin",
    "colinad@piedmonthealth.org": "Piedmont Health",
    "dina.jensen@communitycaretx.org": "CommUnityCare",
    "klampher@hcrs.org": "HCRS",
    "jcochran@keystonehealth.org": "Keystone Health",
    "elizabethd@elitedna.com": "Elite DNA Behavioral Health",
    "tostrom@encorecares.com": "Encore Senior Living",
    "mbennion@signaturedentalpartners.com": "Signature Dental Partners",
    "fbrown@nmhs.net": "North Mississippi Health Services",
    "mario.difiglia@wmchealth.org": "WMCHealth",
    "croark@mysagedental.com": "Sage Dental",
    "matthew.polk@wacofamilymedicine.org": "Waco Family Medicine",
    "jacob@thecaregivingcompany.com": "The Caregiving Company",
    "stephanie@adamjadeventures.com": "Adam Jade Ventures",
    "joseph.rudisill@hcahealthcare.com": "HCA Florida Doctors Hospital",
    "gmay@concern4kids.org": "Concern",
}


def site_key(row_cells):
    def c(i):
        return row_cells[i].strip() if len(row_cells) > i and row_cells[i] else ""
    addr = UNIT_RE.sub("", c(C_ADDR).upper()).strip(" ,.")
    return (addr or c(C_COMPANY).upper(), c(C_ZIP)[:5] or c(C_CITY).upper())


def brand_for(email, rows):
    if email in BRAND_BY_EMAIL:
        return BRAND_BY_EMAIL[email]
    def c(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""
    names = [c(r, C_COMPANY) for r in rows
             if c(r, C_COMPANY) and not c(r, C_COMPANY)[0].isdigit()]
    if not names:
        names = [c(r, C_PARENT) for r in rows if c(r, C_PARENT)]
    base = re.split(r"\s+at\s+", names[0] if names else "", maxsplit=1, flags=re.I)[0]
    return casual_company(base)


def build_multi_bodies(values):
    """email -> (Variant C body, site count) for DMs covering 2+ distinct addresses."""
    from collections import defaultdict, Counter
    def c(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""
    groups = defaultdict(list)
    for r in values:
        em = c(r, C_EMAIL).lower()
        if em:
            groups[em].append(r)
    out = {}
    for em, rows in groups.items():
        sites = {site_key(r) for r in rows}
        if len(sites) < 2:
            continue
        r0 = rows[0]
        cities = sorted({casual_city(c(r, C_CITY)) for r in rows if c(r, C_CITY)})
        states = sorted({c(r, C_STATE) for r in rows if c(r, C_STATE)})
        if len(cities) == 1:
            where = f" in {cities[0]}"
        elif len(cities) == 2:
            where = f" across {cities[0]} and {cities[1]}"
        elif len(states) == 1:
            where = f" across {STATE_NAMES.get(states[0], states[0])}"
        else:
            where = ""
        gtype = Counter(phrases(c(r, C_TAX))[1] for r in rows).most_common(1)[0][0]
        tpl = VARIANT_C if gtype else VARIANT_C_GENERIC
        out[em] = (tpl.format(first=casual_first(c(r0, C_FIRST)),
                              company=brand_for(em, rows), where=where,
                              gtype=gtype or ""), len(sites))
    return out


def render(row_cells):
    def c(i):
        return row_cells[i].strip() if len(row_cells) > i and row_cells[i] else ""
    first = casual_first(c(C_FIRST))
    company = casual_company(c(C_COMPANY))
    city = casual_city(c(C_CITY))
    ptype, gtype, unit = phrases(c(C_TAX))
    is_new = c(C_STATUS_LEAD).startswith("NEW PRACTICE")
    if is_new:
        tpl = VARIANT_A if ptype else VARIANT_A_GENERIC
        return tpl.format(first=first, company=company, city=city,
                          ptype=ptype or "", unit=unit or ""), "A"
    tpl = VARIANT_B if gtype else VARIANT_B_GENERIC
    return tpl.format(first=first, company=company, city=city,
                      gtype=gtype or ""), "B"


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
    ap.add_argument("--preview", type=int, default=0,
                    help="render N bodies to stdout, write NOTHING")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    svc = get_service()
    sid = args.sheet_url.split("/d/")[1].split("/")[0]
    values = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:AI").execute().get("values", [])

    def c(r, i):
        return r[i].strip() if len(r) > i and r[i] else ""

    todo = [(n, r) for n, r in enumerate(values, start=2)
            if c(r, C_EMAIL) and not c(r, C_BODY)]
    multi = build_multi_bodies(values)
    n_multi_rows = sum(1 for _, r in todo if c(r, C_EMAIL).lower() in multi)
    print(f"[gen] {len(todo)} rows need a body | {len(multi)} multi-site DMs "
          f"(Variant C, {n_multi_rows} rows)")

    def render_row(r):
        em = c(r, C_EMAIL).lower()
        if em in multi:
            return multi[em][0], "C"
        return render(r)

    if args.preview:
        import random
        random.shuffle(todo)
        shown = {}
        for n, r in todo:
            body, var = render_row(r)
            key = (var, phrases(c(r, C_TAX))[0] or "generic")
            if shown.get(key):
                continue
            shown[key] = True
            print(f"\n{'='*62}\nrow {n} | VARIANT {var} | {c(r,C_FTYPE)[:44]} | {c(r,C_STATUS_LEAD)}")
            print(f"{'='*62}\n{body}")
            if len(shown) >= args.preview:
                break
        print(f"\n[gen] PREVIEW ONLY — nothing written")
        return

    if args.limit:
        todo = todo[:args.limit]
    updates, done = [], 0
    for n, r in todo:
        body, _ = render_row(r)
        updates.append({"range": f"'{TAB}'!Z{n}", "values": [[body]]})
        done += 1
        if len(updates) >= 10:               # batch-of-10
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
            updates = []
            print(f"  written {done}/{len(todo)}", end="\r")
            time.sleep(0.4)
    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": updates}).execute()
    print(f"\n[gen] {done} bodies written to col Z")


if __name__ == "__main__":
    main()
