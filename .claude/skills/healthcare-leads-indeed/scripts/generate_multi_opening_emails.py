"""
Generate copy for the Multiple Openings tab (healthcare Indeed sheet).

No icebreaker. The hook is the volume + specialty spread + geography. Per-company the
script extracts the specialty of each distinct posting (GPT-4.1, cached by title),
aggregates distinct specialties, and renders by how many there are:

  1 specialty     -> "5 psychiatric NP roles open across California"
                     experience: "5-7+ years of psychiatric experience"
  2-3 specialties -> "6 NP roles spanning dermatology, primary care and pediatrics open across ..."
                     experience: "the right experience" (vague — many areas)
  4+ specialties  -> "4 NP roles open across {cities}"
                     experience: "the right experience"
  0 specialty     -> "4 NP and PA roles open across {cities}"
                     experience: "5-7+ years of {derived} experience"
  count == 1      -> singular single-opening style hook (the 7 dedupe stragglers)

Writes Email Body -> col AJ, Subject -> col AK, Role label -> col AB. No CTA.
Dedupe by company. Resume-safe with --resume.

Usage:
  python3 -W ignore generate_multi_opening_emails.py --sheet_url "URL" --tab "Multiple Openings" [--preview 8] [--resume]
"""

import os
import re
import json
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

COL_JOB_TITLE = 1   # B
COL_JOB_DESC  = 9   # J
COL_COMPANY   = 10  # K
COL_CITY      = 17  # R
COL_STATE     = 18  # S
COL_DM_NAME   = 19  # T
COL_EMAIL     = 22  # W
COL_ROLE      = 27  # AB  (write role label for follow-up {{Role}})
COL_EMAIL_BODY = 35 # AJ
COL_SUBJECT    = 36 # AK
LLM_WORKERS = 8

ST_FULL = {"GA":"Georgia","TX":"Texas","CA":"California","AZ":"Arizona","IL":"Illinois",
           "NC":"North Carolina","IN":"Indiana","NY":"New York","MD":"Maryland","FL":"Florida"}
EXP_MAP = {"family":"family medicine","internal":"internal medicine","psych":"psychiatric",
           "pain":"pain management","bariatric":"bariatric medicine","ob":"OB/GYN",
           "womens health":"women's health","icu":"ICU","ob-gyn":"OB/GYN"}

SPEC_SYS = ('Extract clinical specialty + credential from a healthcare job title. '
            'Return JSON {"specialty":"<1-2 word area or empty>","credential":"NP|PA"}. '
            'Combos -> NP. Never "Physician". specialty examples: oncology, psychiatric, family, '
            'dermatology, wound care, pain management, cardiology, primary care, urgent care, '
            "women's health, pediatric, neurology, orthopedics. Empty if none clear.")


def safe_get(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    r = ""; idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26); r = chr(65 + rem) + r
    return r


def full_state(s):
    return ST_FULL.get((s or "").strip().upper(), (s or "").strip())


def expand(spec):
    s = (spec or "").strip().lower()
    return EXP_MAP.get(s, s)


def title_case(s):
    out = []
    for w in s.split():
        out.append(w if (w.isupper() or "/" in w) else w[:1].upper() + w[1:])
    return " ".join(out)


def first_name(n):
    n = re.sub(r"\b(Dr\.?|MD|DO|NP|PA|RN|PhD|MBA|MPH|Jr\.?|Sr\.?|II|III|IV|FNP|APRN|DNP|MSN)\b", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\([^)]*\)", "", n); n = re.sub(r",", " ", n); n = re.sub(r"\s+", " ", n).strip()
    p = n.split(); return p[0] if p else "there"


def join_and(items):
    items = list(dict.fromkeys(items))
    if not items: return ""
    if len(items) == 1: return items[0]
    if len(items) == 2: return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def article(phrase):
    p = (phrase or "").strip().lower()
    if not p: return "a"
    if p.startswith("np"): return "an"
    return "an" if p[0] in "aeiou" else "a"


def get_client():
    return AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION)


_spec_cache = {}
def extract_spec(client, title):
    if title in _spec_cache:
        return _spec_cache[title]
    try:
        resp = client.chat.completions.create(model=AZURE_DEPLOYMENT, max_tokens=40, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SPEC_SYS}, {"role": "user", "content": f'Job title: "{title}"'}])
        d = json.loads(resp.choices[0].message.content or "{}")
        s = expand((d.get("specialty") or "").strip().lower())
        c = (d.get("credential") or "NP").upper()
        if c not in ("NP", "PA"): c = "NP"
        _spec_cache[title] = (s, c)
        return s, c
    except Exception:
        return "", "NP"


def derive_spec(client, title, desc):
    try:
        resp = client.chat.completions.create(model=AZURE_DEPLOYMENT, max_tokens=20, temperature=0,
            messages=[{"role": "user", "content": (
                f"Job title: {title}\nJob description (first 300): {desc[:300]}\n\n"
                "Return ONLY a 2-4 word clinical specialty phrase (e.g. family medicine, primary care, "
                "urgent care). No explanation.")}])
        return (resp.choices[0].message.content or "").strip().strip(".") or "primary care"
    except Exception:
        return "primary care"


def build(client, dm_name, posts):
    """posts = list of (title, city, state, job_desc). Returns (body, subject, role_label)."""
    distinct = list(dict.fromkeys((t, c) for t, c, s, d in posts))
    count = len(distinct)
    specs, creds = [], set()
    for t, c in distinct:
        s, cr = extract_spec(client, t)
        creds.add(cr)
        if s: specs.append(s)
    specs = list(dict.fromkeys(specs))
    cities = list(dict.fromkeys(c for _, c, _, _ in posts))
    states = list(dict.fromkeys(full_state(s) for _, _, s, _ in posts))
    cred = "NP and PA" if {"NP", "PA"} <= creds else ("PA" if creds == {"PA"} else "NP")
    cred_pl = "NPs and PAs" if cred == "NP and PA" else ("PAs" if cred == "PA" else "NPs")
    fn = first_name(dm_name)
    # Location rule: mention only if <=3 (cities preferred, then states); otherwise omit.
    if len(cities) <= 3:
        loc = f"across {join_and(cities)}"
    elif len(states) <= 3:
        loc = f"across {join_and(states)}"
    else:
        loc = ""
    statenames = join_and(states)
    licensed = f"licensed in {statenames} " if len(states) <= 3 else ""

    # ---- count == 1: singular (the dedupe stragglers) ----
    if count == 1:
        spec = specs[0] if specs else ""
        role_first = f"{spec} {cred}".strip() if spec else cred
        art = article(role_first)
        exp = spec if spec else derive_spec(client, distinct[0][0], posts[0][3])
        city = cities[0] if cities else ""
        locp = f"in {city}" if city else (f"in {statenames}" if statenames else "")
        body = (f"Hey {fn},\n\n"
                f"Noticed you're looking for {art} {role_first} {locp}. Is this {cred} hire a priority "
                f"in the next 14-30 days?\n\n"
                f"I know someone who places NPs and PAs in practices like yours. Just spoke with them on "
                f"the phone before sending this, and they mentioned having a few {cred_pl} licensed in "
                f"{statenames} with 5-7+ years of {exp} experience.")
        subj = f"Pre-vetted {title_case(spec)} Nurse Practitioners" if (spec and cred != "PA") else \
               (f"Pre-vetted {title_case(spec)} Physician Assistants" if spec else
                ("Pre-vetted Physician Assistants" if cred == "PA" else "Pre-vetted Nurse Practitioners"))
        role_label = role_first
        return body.replace("—", ",").replace("–", ","), subj, role_label

    # ---- count >= 2 ----
    n = len(specs)
    if n == 1:
        role_phrase = f"{count} {specs[0]} {cred} roles"
        exp_clause = f"with 5-7+ years of {specs[0]} experience"
        subj = (f"Pre-vetted {title_case(specs[0])} Nurse Practitioners" if cred != "PA"
                else f"Pre-vetted {title_case(specs[0])} Physician Assistants")
        role_label = f"{specs[0]} {cred}" if cred != "NP and PA" else f"{specs[0]} NP"
    elif 2 <= n <= 3:
        role_phrase = f"{count} {cred} roles spanning {join_and(specs)}"
        exp_clause = "with the right experience"
        subj = "Pre-vetted Nurse Practitioners"
        role_label = "NP"
    elif n >= 4:
        role_phrase = f"{count} {cred} roles"
        exp_clause = "with the right experience"
        subj = "Pre-vetted Nurse Practitioners"
        role_label = "NP"
    else:  # no specialty
        role_phrase = f"{count} {cred} roles"
        derived = derive_spec(client, distinct[0][0], posts[0][3])
        exp_clause = f"with 5-7+ years of {derived} experience"
        subj = "Pre-vetted Nurse Practitioners"
        role_label = "NP"

    open_phrase = f"open {loc}" if loc else "open right now"
    body = (f"Hey {fn},\n\n"
            f"Noticed you've got {role_phrase} {open_phrase}. When roles like this go unfilled, it usually "
            f"means your patients aren't getting the care they need, and revenue is walking out the door.\n\n"
            f"I know someone who places NPs and PAs in practices like yours. Just spoke with them on the "
            f"phone before sending this, and they mentioned having a few {cred_pl} {licensed}{exp_clause}.")
    return body.replace("—", ",").replace("–", ","), subj, role_label


def ensure_columns(svc, sid, tab, min_cols):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            gid = s["properties"]["sheetId"]; have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"appendDimension": {
                    "sheetId": gid, "dimension": "COLUMNS", "length": min_cols - have}}]}).execute()
            return


def set_row_height(svc, sid, tab):
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            gid = s["properties"]["sheetId"]; rc = s["properties"]["gridProperties"]["rowCount"]
            svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": rc},
                "properties": {"pixelSize": 18}, "fields": "pixelSize"}}]}).execute()
            return


def main():
    ap = argparse.ArgumentParser(description="Generate Multiple Openings copy")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Multiple Openings")
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    svc = get_google_service(); sid = get_sheet_id_from_url(args.sheet_url)
    client = get_client()
    print(f"=== Generate Multiple Openings copy ({'PREVIEW' if args.preview else 'LIVE'}) — {args.tab} ===\n")

    rows = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{args.tab}'!A2:AK5000").execute().get("values", [])
    companies = OrderedDict()
    for i, r in enumerate(rows):
        co = safe_get(r, COL_COMPANY); dm = safe_get(r, COL_DM_NAME); email = safe_get(r, COL_EMAIL)
        if not co or not dm or not email:
            continue
        if args.resume and safe_get(r, COL_EMAIL_BODY):
            continue
        companies.setdefault(co, {"dm": dm, "rows": [], "posts": []})
        companies[co]["rows"].append(i + 2)
        companies[co]["posts"].append((safe_get(r, COL_JOB_TITLE), safe_get(r, COL_CITY),
                                       safe_get(r, COL_STATE), safe_get(r, COL_JOB_DESC)))

    print(f"Companies to generate: {len(companies)}")

    def run(co):
        info = companies[co]
        body, subj, role = build(client, info["dm"], info["posts"])
        return co, body, subj, role

    if args.preview:
        for co in list(companies)[:args.preview]:
            _, body, subj, role = run(co)
            print(f"=== {co}  [role={role}] ===")
            print(f"SUBJECT: {subj}")
            print(body)
            print()
        print("[PREVIEW] No writes.")
        return

    ensure_columns(svc, sid, args.tab, COL_SUBJECT + 1)
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
        {"range": f"'{args.tab}'!{col_letter(COL_EMAIL_BODY)}1", "values": [["Email Body"]]},
        {"range": f"'{args.tab}'!{col_letter(COL_SUBJECT)}1", "values": [["Subject"]]},
    ]}).execute()

    pending, done, written = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run, co): co for co in companies}
        for fut in as_completed(futs):
            co, body, subj, role = fut.result()
            done += 1
            for rn in companies[co]["rows"]:
                pending += [
                    {"range": f"'{args.tab}'!{col_letter(COL_EMAIL_BODY)}{rn}", "values": [[body]]},
                    {"range": f"'{args.tab}'!{col_letter(COL_SUBJECT)}{rn}", "values": [[subj]]},
                    {"range": f"'{args.tab}'!{col_letter(COL_ROLE)}{rn}", "values": [[role]]},
                ]
            written += 1
            if len(pending) >= 90 or done == len(companies):
                svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": pending}).execute()
                pending = []
                time.sleep(0.3)
            if done % 25 == 0:
                print(f"  {done}/{len(companies)} done")

    set_row_height(svc, sid, args.tab)
    print(f"\nDone. {written} companies written (body AJ, subject AK, role AB).")


if __name__ == "__main__":
    main()
