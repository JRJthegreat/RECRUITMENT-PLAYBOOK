"""
Generate specialty-specific email bodies for an opening tab (healthcare Indeed sheet).

Uses the approved framework with specialty up front + in the experience line:

  Hey {first_name},

  {icebreaker}

  Noticed you're looking for {article} {role_first} in {city}. Is this {credential} hire a
  priority in the next 14-30 days?

  Asking because I know someone who's filled {credential} roles for practices similar to
  yours. Just spoke with them on the phone before sending this, and they mentioned having a
  few {credential}s with 5-7+ years of {specialty} experience looking for roles in {ST} right now.

Role comes from col AB (e.g. "oncology NP", "NP", "dermatology PA"), written by
clean_specific_role.py. credential = last token; specialty = remainder. When the role has no
specialty, the experience-line specialty is derived from the job description (fallback "primary care").
No CTA line (Jude adds it). No em dashes. Dedupe by company, resume-safe.

Usage:
  python3 -W ignore generate_opening_emails.py --sheet_url "URL" --tab "Single Opening" [--preview 8]
"""

import os
import re
import time
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI

from pull_dataset import get_google_service, get_sheet_id_from_url

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

COL_JOB_TITLE  = 1   # B
COL_JOB_DESC   = 9   # J
COL_COMPANY    = 10  # K
COL_CITY       = 17  # R
COL_STATE      = 18  # S
COL_DM_NAME    = 19  # T
COL_EMAIL      = 22  # W
COL_ROLE       = 27  # AB
COL_ICEBREAKER = 34  # AI
COL_EMAIL_BODY = 35  # AJ
COL_SUBJECT    = 36  # AK
LLM_WORKERS    = 8

CRED_FULL = {"NP": "Nurse Practitioner", "PA": "Physician Assistant"}

STATE_ABBREV = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO",
    "Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID",
    "Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
    "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
    "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
    "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN","Texas":"TX",
    "Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV",
    "Wisconsin":"WI","Wyoming":"WY",
}


def safe_get(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_gid(svc, sid, tab):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"], s["properties"]["gridProperties"]["rowCount"]
    return None, None


def ensure_columns(svc, sid, tab, min_cols):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"appendDimension": {
                    "sheetId": gid, "dimension": "COLUMNS", "length": min_cols - have}}]}).execute()
            return


def split_role(role):
    """'oncology NP' -> ('oncology', 'NP').  'NP' -> ('', 'NP')."""
    role = (role or "NP").strip()
    parts = role.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].upper() in ("NP", "PA"):
        return parts[0].strip(), parts[1].upper()
    if role.upper() in ("NP", "PA"):
        return "", role.upper()
    return "", "NP"


def article_for(phrase):
    p = (phrase or "").strip().lower()
    if not p:
        return "a"
    if p.startswith("np"):       # "en" sound
        return "an"
    return "an" if p[0] in "aeiou" else "a"


def first_name(full_name):
    if not full_name:
        return "there"
    name = re.sub(r"\b(Dr\.?|MD|PhD|DO|NP|PA|RN|MBA|MPH|Jr\.?|Sr\.?|II|III|IV|CHCR|MSN|LPC|LCSW|FNP|APRN)\b",
                  "", full_name, flags=re.IGNORECASE)
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r",", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split()
    return parts[0] if parts else "there"


# Some bare specialties read awkwardly in "5-7+ years of X experience" — expand them.
EXP_SPECIALTY_MAP = {
    "family": "family medicine",
    "internal": "internal medicine",
    "geriatric": "geriatrics",
    "psych": "psychiatric",
    "ob": "OB/GYN",
    "neuro": "neurology",
}


ACRONYM_MAP = {"icu": "ICU", "ob-gyn": "OB/GYN", "ob gyn": "OB/GYN", "obgyn": "OB/GYN",
               "ent": "ENT", "er": "ER"}


def normalize_specialty(specialty):
    """Uppercase known clinical acronyms; leave the rest lowercase."""
    s = (specialty or "").strip().lower()
    return ACRONYM_MAP.get(s, s)


def exp_phrase(specialty):
    s = normalize_specialty(specialty)
    return EXP_SPECIALTY_MAP.get(s.lower(), s)


def derive_specialty(client, job_title, job_desc):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=20, temperature=0,
            messages=[{"role": "user", "content": (
                f"Job title: {job_title}\nJob description (first 400 chars): {job_desc[:400]}\n\n"
                "Return ONLY a 2-4 word clinical specialty phrase (e.g. 'family medicine', "
                "'urgent care', 'primary care'). No punctuation, no explanation.")}],
        )
        return (resp.choices[0].message.content or "").strip().strip(".") or "primary care"
    except Exception:
        return "primary care"


def title_specialty(s):
    """Title-case a specialty, preserving acronyms (ICU, OB/GYN) and apostrophes (women's)."""
    out = []
    for w in s.split():
        if w.isupper() or "/" in w:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def build_subject(role):
    """'oncology NP' -> 'Pre-vetted Oncology Nurse Practitioners'."""
    specialty, cred = split_role(role)
    specialty = normalize_specialty(specialty)
    cred_full = CRED_FULL.get(cred, "Nurse Practitioner") + "s"
    if specialty:
        return f"Pre-vetted {title_specialty(specialty)} {cred_full}"
    return f"Pre-vetted {cred_full}"


def build_email(first, icebreaker, role, city, state, exp_specialty):
    specialty, cred = split_role(role)
    specialty = normalize_specialty(specialty)
    role_first = f"{specialty} {cred}".strip() if specialty else cred
    art = article_for(role_first)
    plural = cred + "s"
    abbrev = STATE_ABBREV.get(state, state[:2].upper() if state else "")
    loc = f"in {city}" if city else (f"in {abbrev}" if abbrev else "")
    state_str = f"in {abbrev}" if abbrev else ""
    body = (
        f"Hey {first},\n\n{icebreaker}\n\n"
        f"Noticed you're looking for {art} {role_first} {loc}. "
        f"Is this {cred} hire a priority in the next 14-30 days?\n\n"
        f"Asking because I know someone who's filled {cred} roles for practices similar to yours. "
        f"Just spoke with them on the phone before sending this, and they mentioned having a few "
        f"{plural} with 5-7+ years of {exp_specialty} experience looking for roles {state_str} right now."
    )
    return re.sub(r"\s+\.", ".", body).replace("—", ",").replace("–", ",")


def main():
    ap = argparse.ArgumentParser(description="Generate specialty-specific opening emails")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="Skip rows that already have an email body")
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    svc = get_google_service()
    sid = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)

    print(f"=== Generate Opening Emails ({'PREVIEW' if args.preview else 'LIVE'}) — {args.tab} ===\n")

    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A2:AJ5000"
    ).execute().get("values", [])

    # dedupe by company; require DM + email + icebreaker
    companies = OrderedDict()
    for i, r in enumerate(rows):
        co = safe_get(r, COL_COMPANY); dm = safe_get(r, COL_DM_NAME)
        email = safe_get(r, COL_EMAIL); ice = safe_get(r, COL_ICEBREAKER)
        if not co or not dm or not email or not ice:
            continue
        if args.resume and safe_get(r, COL_EMAIL_BODY):
            continue
        if co not in companies:
            companies[co] = {"row": r, "sheet_rows": []}
        companies[co]["sheet_rows"].append(i + 2)

    print(f"Companies to generate: {len(companies)}")

    def run(co):
        r = companies[co]["row"]
        role = safe_get(r, COL_ROLE) or "NP"
        specialty, _ = split_role(role)
        exp = exp_phrase(specialty) if specialty else derive_specialty(client, safe_get(r, COL_JOB_TITLE), safe_get(r, COL_JOB_DESC))
        body = build_email(
            first_name(safe_get(r, COL_DM_NAME)),
            safe_get(r, COL_ICEBREAKER),
            role,
            safe_get(r, COL_CITY),
            safe_get(r, COL_STATE),
            exp,
        )
        subject = build_subject(role)
        return co, body, subject

    if args.preview:
        for co in list(companies)[:args.preview]:
            _, body, subject = run(co)
            print(f"=== {co} ===")
            print(f"SUBJECT: {subject}")
            print(body)
            print()
        print("[PREVIEW] No writes.")
        return

    ensure_columns(svc, sid, args.tab, COL_SUBJECT + 1)
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
            {"range": f"'{args.tab}'!{col_letter(COL_EMAIL_BODY)}1", "values": [["Email Body"]]},
            {"range": f"'{args.tab}'!{col_letter(COL_SUBJECT)}1", "values": [["Subject"]]},
        ]}).execute()

    pending, done, written = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run, co): co for co in companies}
        for fut in as_completed(futs):
            co, body, subject = fut.result()
            done += 1
            for rn in companies[co]["sheet_rows"]:
                pending.append({"range": f"'{args.tab}'!{col_letter(COL_EMAIL_BODY)}{rn}", "values": [[body]]})
                pending.append({"range": f"'{args.tab}'!{col_letter(COL_SUBJECT)}{rn}", "values": [[subject]]})
            written += 1
            if len(pending) >= 50 or done == len(companies):
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=sid, body={"valueInputOption": "RAW", "data": pending}).execute()
                pending = []
                time.sleep(0.3)
            if done % 50 == 0:
                print(f"  {done}/{len(companies)} done")

    gid, rc = get_gid(svc, sid, args.tab)
    if gid is not None:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": rc},
            "properties": {"pixelSize": 18}, "fields": "pixelSize"}}]}).execute()

    print(f"\nDone. {written} email bodies written to col {col_letter(COL_EMAIL_BODY)}.")


if __name__ == "__main__":
    main()
