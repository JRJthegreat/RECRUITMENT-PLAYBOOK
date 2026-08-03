"""
Apollo DM Waterfall — find decision maker + verified email for ~1 AMF credit/company.

Per row (has website, no email yet):
  1. Apollo api_search by domain (FREE, no credits) -> candidate people
     (first_name, last_name_obfuscated "Xx***x", title, has_email).
     Large orgs (size >= 500) are searched WITH a person_titles filter
     (owner/CEO + region-scoped ops) so the 100-person page contains
     relevant people.
  2. Azure OpenAI GPT-4.1 ranks up to 3 candidates (P1-P3) against a fixed
     ladder (Jude, 2026-07-31): CEO/Owner -> company-level COO -> a genuine
     company-level Medical Director -> nobody. A row with no one on that
     ladder is left untouched rather than enriched with a lesser title.
     HR at any level, site/regional ops managers, administrators and
     practice managers are never picked. Lists are capped at 500 employees
     upstream in process_city_scrape.py.
  3. Per candidate until one verified email:
       a. Google (apify~google-search-scraper) "{first} {company} {title}
          site:linkedin.com/in" -> de-obfuscate last name (validated against
          the Xx***x pattern) + LinkedIn URL.
       b. AMF /find-email/person with full name + domain (1 credit ONLY when
          a verified email is found; misses are free).
       c. Google failed -> AMF with "{first} {last-initial}" (catches
          first-name email formats, e.g. steli@close.io).
  4. Writes DM Name / DM Title / LinkedIn / Email / First / Last + status col.
     NEVER writes DM data without a valid email (no partial rows).

Rows are processed in chunks of 10 (CHUNK_WORKERS concurrent); the sheet is
written after each chunk (crash-safe, idempotent — rows with a non-empty
email or status are skipped on rerun).

Usage:
  python3 -W ignore apollo_dm_waterfall.py --sheet_url "URL" [options]

  --tab Leads            worksheet name
  --col_company K        company name column (letter)
  --col_website L        website/domain column
  --col_size M           company size column (drives DM targeting rule)
  --col_dm_name T --col_dm_title U --col_linkedin V --col_email W
  --col_first X --col_last Y
  --col_status AB        waterfall status/method column (header auto-set)
  --limit 30             max rows to process this run
  --dry_run              plan only, no API calls or writes
"""

import os
import re
import sys
import json
import time
import argparse
import threading
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
AMF_API_KEY = os.getenv("ANYMAILFINDER_API_KEY")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.1/find-email/person"
GOOGLE_ACTOR_URL = ("https://api.apify.com/v2/acts/apify~google-search-scraper"
                    "/run-sync-get-dataset-items")

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

BATCH_SIZE = 10
CHUNK_WORKERS = 5
LARGE_ORG_MIN = 500

# --skip_email: stop at the DM's identity and never call AnyMail Finder.
# Apollo search + Google de-obfuscation already yield first name, real last
# name, title and LinkedIn URL before any AMF call, so this costs 0 AMF
# credits. Deliberately breaks the pipeline's usual "never write DM data
# without a valid email" rule — rows land with identity but a blank Email,
# marked dm_only_* in the status column so a later email pass can find them.
SKIP_EMAIL = False

# --skip_large filters at QUEUE time on the sheet's size column. Blank-size rows
# band TINY there and are only revealed as 500+ later, by the free Apollo
# proxy inside process_lead — at which point the row was already being enriched.
# This global lets that later discovery honour the same intent.
SKIP_LARGE = False

# --admin_rescue: a SECOND pass for rows where the owner-first ladder found
# nobody. Jude's call (2026-08-01), taken against the evidence and knowing it:
# admin/ops titles are 0 replies from ~120 contacts pooled across four
# campaigns (Practice Administrator 0/5, Administrator 0/26, Executive Director
# 0/21, Director of Operations 0/23, COO 0/43). Apollo DOES index these people
# at small clinics — precisely where it has no owner — so this buys coverage
# the main ladder cannot reach.
#
# It is kept as a separate flag, a separate prompt and a separate status prefix
# (dm_admin_*) so the results stay MEASURABLE. If these rows reply, the ban was
# wrong and we will see it; if they do not, nothing contaminated the owner rows.
ADMIN_RESCUE = False

ADMIN_TITLES = [
    "practice administrator", "administrator", "practice manager",
    "office manager", "executive director", "director of operations",
    "chief operating officer", "coo", "operations manager",
    "business manager", "clinic manager", "director of business operations",
]

ADMIN_RANK_SYSTEM = """You rank employees of a healthcare organization for a \
RESCUE pass. The owner-first ladder (CEO -> COO -> Medical Director) already \
ran on this company and found nobody, so the usual bans are relaxed.

Pick the most senior BUSINESS-OPERATIONS leader at this specific company, in
this order:
1. Practice Administrator / Administrator / Executive Director
2. Chief Operating Officer / Director of Operations / Director of Business Operations
3. Practice Manager / Office Manager / Clinic Manager / Business Manager
4. Nobody — return an empty list.

Still NEVER pick:
- HR of any level, talent acquisition, internal recruiters.
- Clinical staff with no business authority (nurses, therapists, technologists,
  medical assistants, front desk, billing clerks, schedulers).
- Anyone whose title shows they work for a DIFFERENT company.

Prefer company-level over single-site scope when both appear. Prefer candidates
with has_email true when priorities tie.

Reply with ONLY JSON: {"picks": [<index>, ...]}"""

# Server-side title filter for large orgs — without it, api_search returns an
# arbitrary 100 of thousands of employees and the decision maker rarely makes
# the page. Must mirror the RANK_SYSTEM ladder exactly (CEO -> COO -> Medical
# Director) or the ranker gets a page with nobody valid on it. ALL other
# clinical titles and ALL HR titles were removed on 2026-07-31: chief clinical
# 0/126, HR 0/32.
LARGE_ORG_TITLES = [
    "chief executive officer", "ceo", "president", "founder", "co-founder",
    "owner", "managing partner", "managing director", "principal",
    "chief operating officer", "coo",
    "medical director", "chief medical director",
]

RANK_SYSTEM = """You rank employees of a healthcare organization by who most likely \
owns the decision to engage an external recruitment firm to fill the OPEN ROLE \
given (placement fees are a $15-30k spend decision — pick people with budget \
authority, not hiring-logistics coordinators).

DECISION LADDER (Jude, 2026-07-31 — final). Work strictly down this list and
stop at the first person you can find:

1. CEO / Owner / Co-Owner / Founder / Co-Founder / President / Managing
   Partner / Principal / Proprietor. The top of the house.
2. COO / Chief Operating Officer. Company-level only, not a site or regional
   operations manager.
3. Medical Director — but ONLY an employed, company-level Medical Director
   who is part of the leadership of THIS organisation. At small clinics a
   standalone "Medical Director" is very often a contracted outside physician
   holding the licence for compliance, with no involvement in hiring and no
   spend authority: do NOT pick those. Signals it is the real thing: the
   title is paired with an executive or ownership role ("Owner & Medical
   Director", "Founder / Medical Director", "Chief Medical Director"), or the
   person clearly runs the practice. If you cannot tell, skip it.
4. Nobody. Return an EMPTY LIST.

Never invent a fourth rung. If none of CEO, COO or a genuine Medical Director
is on the candidate list, returning nothing is the correct answer — a lesser
title spends an AMF credit, burns the company, and does not reply.

WHY THIS ORDER, and what the evidence actually says. Measured across four
healthcare demand campaigns (1,209 leads, Jul 2026): Owner/CEO/Founder
replied at 2.80%, every other title combined at 0.67% (4.2x, Fisher p=0.004),
and EVERY interested reply came from an owner. Rungs 2 and 3 are Jude's
judgement calls for coverage when no CEO exists, not findings — COO/Ops has 0
replies from 87 contacts and clinical leadership has 0 interested replies from
307. Rank them in the order given, but never above the CEO.

The old discipline-matching ladder (CMO for physician roles, CNO for nursing,
Director of Pharmacy for pharmacists, Director of Rehab for therapy...) is
DELETED. It was measured and it failed: chief-level clinical went 0 for 126
(CMO 0/78, CNO and Director of Nursing 0/29, Chief Clinical Officer 0/19). Do
not reconstruct it under any circumstances.

The list is capped at 500 employees upstream, so almost everything you see
will be a small or mid-size independent employer. TINY orgs (<50, or size
unknown) are the best-performing band at 3.52% and it is the owner who
answers there.

NEVER pick, at any size, for any reason:
- HR of any level — CHRO, Chief People Officer, VP/Director of HR, TA staff,
  internal recruiters. 0 replies from 32 contacts; a recruitment-agency pitch
  reads as replacing their function.
- Any clinical title other than a genuine company-level Medical Director per
  rung 3 — no CMO, no CNO, no Director of Nursing, no Chief Clinical Officer,
  no Clinical Director, no Director of Rehab/Therapy/Pharmacy/Radiology, no
  Laboratory Director, no EMS Chief.
- Site or regional operations managers, Administrators, Executive Directors
  of a single facility. Company-level COO only.
- Practice Manager, Office Manager, Clinic Manager, Scheduling Manager or
  Coordinator, Staffing Coordinator, HR Coordinator/Generalist — they run
  hiring logistics but cannot approve vendor spend.

Multi-location organizations: location/region-scoped titles (Regional Manager,
Area Director, "Director - <region>", branch/site roles) only count if their
region plausibly covers the JOB LOCATION given; when in doubt prefer
company-level executives — owners and C-level speak for every location.

Rules:
- Skip pure clinical staff with no admin authority (staff nurses, techs, therapists, hygienists, associate dentists/physicians).
- Skip people whose title is unrelated to this company or clearly a different employer.
- Prefer candidates with has_email true when priorities tie.
- Return at most 3, best first. If nobody qualifies, return an empty list.

Reply with ONLY JSON: {"picks": [<index>, ...]}"""


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def idx_to_col(idx):
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def get_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    return build("sheets", "v4", credentials=creds)


def get_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        sys.exit("Bad sheet URL")
    return m.group(1)


def norm_domain(website):
    w = (website or "").strip().lower()
    if not w:
        return ""
    if not w.startswith("http"):
        w = "https://" + w
    host = urlparse(w).netloc or ""
    return host[4:] if host.startswith("www.") else host


def size_band(size_str):
    """TINY (<50 or unknown) / MID (50-499) / LARGE (500+)."""
    m = re.search(r"([\d,]+)", size_str or "")
    if not m:
        return "TINY"
    lb = int(m.group(1).replace(",", ""))
    return "LARGE" if lb >= 500 else ("MID" if lb >= 50 else "TINY")


def parse_obfuscation(obf):
    """'Wo***e' -> ('Wo', 'e'). Returns (prefix, suffix) or (None, None)."""
    m = re.match(r"^([A-Za-z]+)\*+([A-Za-z]+)$", (obf or "").strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def surname_matches(surname, prefix, suffix):
    s = (surname or "").lower()
    return (len(s) >= len(prefix) + len(suffix)
            and s.startswith(prefix.lower()) and s.endswith(suffix.lower()))


def clean_person_name(raw):
    """'Dr. Jane Smith, MD' -> ('Jane', 'Smith'). Returns (first, last) or (None, None)."""
    n = re.sub(r"\b(dr|md|do|dds|dmd|np|pa|rn|phd|mba)\b\.?", "", (raw or ""),
               flags=re.I)
    n = n.split(",")[0].strip()
    parts = [p for p in n.split() if p]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[-1]


# --- Stage 1: Apollo ---

def apollo_search(domain, titles=None):
    payload = {"q_organization_domains_list": [domain],
               "page": 1, "per_page": 100}
    if titles:
        payload["person_titles"] = titles
    for attempt in (1, 2):
        try:
            resp = requests.post(
                APOLLO_SEARCH_URL,
                headers={"x-api-key": APOLLO_API_KEY,
                         "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
        except requests.RequestException as e:
            print(f"    [!] apollo {domain}: {type(e).__name__}")
            return None, 0
        if resp.status_code == 429 and attempt == 1:
            print("    [!] apollo 429 — sleeping 60s")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            print(f"    [!] apollo {domain}: HTTP {resp.status_code}")
            return None, 0
        data = resp.json()
        return data.get("people", []), data.get("total_entries", 0) or 0
    return None, 0


# --- Stage 2: GPT-4.1 ranking ---

def rank_candidates(client, company, people, band, job_location="",
                    job_title=""):
    lines = []
    for i, p in enumerate(people):
        lines.append(f'{i}: first_name="{p.get("first_name", "")}" '
                     f'title="{p.get("title", "")}" '
                     f'has_email={p.get("has_email", False)}')
    user = (f'Company: "{company}"\n'
            f'ORG SIZE: {band}\n'
            f'OPEN ROLE: {job_title or "unknown"}\n'
            f'JOB LOCATION: {job_location or "unknown"}\n'
            f'People:\n' + "\n".join(lines))
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=60,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system",
                       "content": ADMIN_RANK_SYSTEM if ADMIN_RESCUE else RANK_SYSTEM},
                      {"role": "user", "content": user}],
        )
        picks = json.loads(resp.choices[0].message.content).get("picks", [])
        return [i for i in picks if isinstance(i, int) and 0 <= i < len(people)][:3]
    except Exception as e:
        print(f"    [!] rank: {type(e).__name__}: {e}")
        return []


# --- Stage 3a: Google de-obfuscation ---

def google_deobfuscate(person, company, domain):
    """Returns (surname, linkedin_url) or (None, None)."""
    first = person.get("first_name", "")
    prefix, suffix = parse_obfuscation(person.get("last_name_obfuscated", ""))
    if not first or not prefix:
        return None, None
    title_short = " ".join((person.get("title") or "").split()[:4])
    query = f"{first} {company} {title_short} site:linkedin.com/in"
    try:
        resp = requests.post(
            GOOGLE_ACTOR_URL,
            params={"token": APIFY_TOKEN},
            json={"queries": query, "resultsPerPage": 8, "maxPagesPerQuery": 1,
                  "languageCode": "en", "countryCode": "us",
                  "includeUnfilteredResults": False},
            timeout=180,
        )
    except requests.RequestException as e:
        print(f"    [!] google: {type(e).__name__}")
        return None, None
    if resp.status_code not in (200, 201):
        print(f"    [!] google: HTTP {resp.status_code}")
        return None, None

    company_tokens = {t for t in re.split(r"\W+", company.lower()) if len(t) > 2}
    for item in resp.json():
        for r in item.get("organicResults", []):
            url = r.get("url", "")
            if "linkedin.com/in/" not in url:
                continue
            blob = (r.get("title", "") + " " + r.get("description", "")).lower()
            # Candidate surnames from URL slug and result title
            slug = urlparse(url).path.split("/in/")[-1].strip("/")
            slug_tokens = [re.sub(r"\d+$", "", t) for t in slug.split("-") if t]
            candidates = []
            if slug_tokens and slug_tokens[0].lower() == first.lower():
                candidates.append("".join(slug_tokens[1:]) if len(slug_tokens) == 2
                                  else (slug_tokens[1] if len(slug_tokens) > 1 else ""))
            title_text = r.get("title", "").split(" - ")[0].split(" | ")[0].split(" – ")[0]
            words = [re.sub(r"[^A-Za-z'-]", "", w) for w in title_text.split()]
            words = [w for w in words if w]
            if len(words) >= 2 and words[0].lower() == first.lower():
                candidates.append(words[-1])
            for cand in candidates:
                if cand and surname_matches(cand, prefix, suffix):
                    # loose guard: company or title overlap in the snippet
                    if (any(t in blob for t in company_tokens)
                            or any(t in blob for t in title_short.lower().split() if len(t) > 3)):
                        return cand.title(), url.split("?")[0]
    return None, None


# --- Stage 3b/3c: AMF ---

def amf_find(full_name, domain):
    """Returns (email, credits_charged) — email only when status == valid."""
    try:
        resp = requests.post(
            AMF_PERSON_URL,
            headers={"Authorization": AMF_API_KEY,
                     "Content-Type": "application/json"},
            json={"full_name": full_name, "domain": domain},
            timeout=180,
        )
        data = resp.json()
    except Exception as e:
        print(f"    [!] amf: {type(e).__name__}")
        return None, 0
    credits = data.get("credits_charged", 0) or 0
    if data.get("email_status") == "valid" and data.get("valid_email"):
        return data["valid_email"], credits
    return None, credits


# --- Per-lead pipeline (thread-safe: returns updates, mutates only stats via lock) ---

def process_lead(t, llm, cols, stats, lock):
    c_dm, c_dmt, c_li, c_email, c_first, c_last, c_status, tab, c_size = cols
    status = ""
    updates = []
    people, total = apollo_search(
        t["domain"],
        titles=(ADMIN_TITLES if (ADMIN_RESCUE and t["band"] == "LARGE")
                else LARGE_ORG_TITLES if t["band"] == "LARGE" else None))

    # Free size proxy: blank-size rows banded TINY by default, but Apollo's
    # total_entries (people indexed at the domain) exposes big orgs at no cost.
    if t.get("size_blank") and total > 0:
        # Record the free signal on the sheet so a blank-size row stops being
        # a permanent unknown. This is Apollo's INDEXED-PEOPLE count, not true
        # headcount — it undercounts, and undercounts worst at small practices
        # where few staff have public profiles. The error direction is the
        # safe one for a 500 cap: it may let a big org through, it will not
        # wrongly drop a small one.
        updates.append((f"{tab}!{idx_to_col(c_size)}{t['sheet_row']}", [total]))
        if total >= 60:
            t["band"] = "LARGE" if total >= 300 else "MID"
            if t["band"] == "LARGE" and SKIP_LARGE:
                print(f"    [-] {t['company']}: {total} people indexed -> 500+, "
                      f"skipping (--skip_large)")
                updates.append((f"{tab}!{idx_to_col(c_status)}{t['sheet_row']}",
                                ["skip_large_by_apollo_proxy"]))
                return updates
            print(f"    [i] {t['company']}: blank size but {total} people "
                  f"indexed -> treating as {t['band']}")
            if t["band"] == "LARGE":
                people, total = apollo_search(t["domain"], LARGE_ORG_TITLES)

    if not people:
        status = "no_apollo_people"
        with lock:
            stats["apollo_empty"] += 1
    else:
        picks = rank_candidates(llm, t["company"], people, t["band"],
                                t.get("job_location", ""),
                                t.get("job_title", ""))
        if not picks:
            status = "no_dm_candidates"
            with lock:
                stats["no_candidates"] += 1
        else:
            ceo_first, ceo_last = clean_person_name(t.get("sheet_ceo", ""))
            for rank_pos, pi in enumerate(picks, 1):
                p = people[pi]
                first = p.get("first_name", "")
                prefix, suffix = parse_obfuscation(
                    p.get("last_name_obfuscated", ""))
                surname, li_url = google_deobfuscate(
                    p, t["company"], t["domain"])
                with lock:
                    stats["google_calls"] += 1

                # Sheet CEO col: free full name when the pick IS the CEO
                if (not surname and ceo_first and prefix
                        and first.lower() == ceo_first.lower()
                        and surname_matches(ceo_last, prefix, suffix)):
                    surname = ceo_last.title()

                if SKIP_EMAIL:
                    # Identity-only mode. Require a real surname — writing
                    # "Jeff W." would give the downstream email tool nothing
                    # to work with, so fall through to the next candidate.
                    if not surname:
                        continue
                    row_n = t["sheet_row"]
                    for idx, val in [(c_dm, f"{first} {surname}"),
                                     (c_dmt, p.get("title", "")),
                                     (c_li, li_url or ""),
                                     (c_first, first),
                                     (c_last, surname)]:
                        updates.append(
                            (f"{tab}!{idx_to_col(idx)}{row_n}", [val]))
                    status = (f"dm_admin_p{rank_pos}" if ADMIN_RESCUE
                              else f"dm_only_p{rank_pos}")
                    with lock:
                        stats["dm_only"] = stats.get("dm_only", 0) + 1
                    print(f"    ✓ {t['company']}: {first} {surname} "
                          f"— {p.get('title','')} (identity only, no email)")
                    break

                email = None
                used = ""
                if surname:
                    email, cr = amf_find(f"{first} {surname}", t["domain"])
                    with lock:
                        stats["amf_credits"] += cr
                    used = "google+amf"
                if not email and prefix:
                    email, cr = amf_find(f"{first} {prefix[0]}", t["domain"])
                    with lock:
                        stats["amf_credits"] += cr
                    if email:
                        used = "amf_initial"
                if email:
                    dm_name = (f"{first} {surname}" if surname
                               else f"{first} {prefix[0]}.")
                    last_val = surname or f"{prefix[0]}."
                    row_n = t["sheet_row"]
                    for idx, val in [(c_dm, dm_name),
                                     (c_dmt, p.get("title", "")),
                                     (c_li, li_url or ""),
                                     (c_email, email),
                                     (c_first, first),
                                     (c_last, last_val)]:
                        updates.append(
                            (f"{tab}!{idx_to_col(idx)}{row_n}", [val]))
                    status = f"found_p{rank_pos}_{used}"
                    with lock:
                        stats["emails"] += 1
                    print(f"    ✓ {t['company']}: {dm_name} <{email}> ({status})")
                    break
            if not status:
                status = "not_found"
                with lock:
                    stats["not_found"] += 1

    # Rescue: no email yet but Indeed listed a CEO name — try it directly.
    # TINY/MID only: CEO is never the target at LARGE orgs.
    if (not SKIP_EMAIL
            and status in ("no_apollo_people", "no_dm_candidates", "not_found")
            and t["band"] != "LARGE"):
        ceo_first, ceo_last = clean_person_name(t.get("sheet_ceo", ""))
        if ceo_first:
            email, cr = amf_find(f"{ceo_first} {ceo_last}", t["domain"])
            with lock:
                stats["amf_credits"] += cr
            if email:
                row_n = t["sheet_row"]
                for idx, val in [(c_dm, f"{ceo_first} {ceo_last}"),
                                 (c_dmt, "CEO"),
                                 (c_li, ""),
                                 (c_email, email),
                                 (c_first, ceo_first),
                                 (c_last, ceo_last)]:
                    updates.append(
                        (f"{tab}!{idx_to_col(idx)}{row_n}", [val]))
                status = "found_sheetceo_amf"
                with lock:
                    stats["emails"] += 1
                print(f"    ✓ {t['company']}: {ceo_first} {ceo_last} "
                      f"<{email}> ({status})")

    updates.append((f"{tab}!{idx_to_col(c_status)}{t['sheet_row']}", [status]))
    return updates


# --- Orchestration ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--col_company", default="K")
    ap.add_argument("--col_website", default="L")
    ap.add_argument("--col_size", default="M")
    ap.add_argument("--col_city", default="R")
    ap.add_argument("--col_state", default="S")
    ap.add_argument("--col_ceo", default="O")
    ap.add_argument("--col_job", default="B")
    ap.add_argument("--col_dm_name", default="T")
    ap.add_argument("--col_dm_title", default="U")
    ap.add_argument("--col_linkedin", default="V")
    ap.add_argument("--col_email", default="W")
    ap.add_argument("--col_first", default="X")
    ap.add_argument("--col_last", default="Y")
    ap.add_argument("--col_status", default="AB")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--size_priority", action="store_true", default=True,
                    help="process TINY/unknown, then MID, then LARGE (default on)")
    ap.add_argument("--no_size_priority", dest="size_priority",
                    action="store_false", help="process in plain sheet order")
    ap.add_argument("--skip_large", action="store_true",
                    help="drop 500+ orgs entirely rather than just deprioritise "
                         "them. OFF by default — 500+ has 0 replies from 244 "
                         "sends, but owner/CEO was targeted at only 2 of them, "
                         "so the band is untested rather than disproven.")
    ap.add_argument("--admin_rescue", action="store_true",
                    help="SECOND pass over rows the owner ladder failed, targeting "
                         "practice administrators / ops leaders. Marked dm_admin_* "
                         "so results stay measurable. Against the evidence: those "
                         "titles are 0 replies from ~120 contacts.")
    ap.add_argument("--skip_email", action="store_true",
                    help="Identity only: write DM name/title/LinkedIn and stop. "
                         "No AnyMail Finder calls, 0 AMF credits. Rows are "
                         "marked dm_only_* so an email pass can pick them up.")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    global SKIP_EMAIL, SKIP_LARGE, ADMIN_RESCUE
    SKIP_EMAIL = args.skip_email
    SKIP_LARGE = args.skip_large
    ADMIN_RESCUE = args.admin_rescue

    for name, val in [("APOLLO_API_KEY", APOLLO_API_KEY),
                      ("ANYMAILFINDER_API_KEY", AMF_API_KEY),
                      ("APIFY_API_TOKEN", APIFY_TOKEN),
                      ("AZURE_OPENAI_API_KEY", AZURE_API_KEY)]:
        if not val:
            sys.exit(f"Missing {name} in .env")

    from openai import AzureOpenAI
    llm = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                      api_version=AZURE_API_VERSION)

    sheet_id = get_sheet_id(args.sheet_url)
    service = get_service()

    c_company = col_to_idx(args.col_company)
    c_web = col_to_idx(args.col_website)
    c_size = col_to_idx(args.col_size)
    c_city = col_to_idx(args.col_city)
    c_state = col_to_idx(args.col_state)
    c_ceo = col_to_idx(args.col_ceo)
    c_job = col_to_idx(args.col_job)
    c_dm = col_to_idx(args.col_dm_name)
    c_dmt = col_to_idx(args.col_dm_title)
    c_li = col_to_idx(args.col_linkedin)
    c_email = col_to_idx(args.col_email)
    c_first = col_to_idx(args.col_first)
    c_last = col_to_idx(args.col_last)
    c_status = col_to_idx(args.col_status)
    last_needed = max(c_company, c_web, c_size, c_city, c_state, c_dm, c_dmt,
                      c_li, c_email, c_first, c_last, c_status)

    rng = f"{args.tab}!A1:{idx_to_col(last_needed)}"
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    header, data = rows[0], rows[1:]

    def cell(row, idx):
        return row[idx].strip() if idx < len(row) and row[idx] else ""

    if len(header) <= c_status or not header[c_status].strip():
        if not args.dry_run:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{args.tab}!{idx_to_col(c_status)}1",
                valueInputOption="RAW",
                body={"values": [["dm_status"]]}).execute()

    todo = []
    for i, row in enumerate(data):
        sheet_row = i + 2
        st = cell(row, c_status)
        if ADMIN_RESCUE:
            # target exactly the rows the owner ladder could not fill
            if cell(row, c_dm) or st not in ("no_dm_candidates", "not_found"):
                continue
        elif cell(row, c_email) or st:
            continue  # already processed
        domain = norm_domain(cell(row, c_web))
        if not domain:
            continue
        loc = ", ".join(x for x in (cell(row, c_city), cell(row, c_state)) if x)
        todo.append({"sheet_row": sheet_row, "domain": domain,
                     "company": cell(row, c_company),
                     "job_location": loc,
                     "sheet_ceo": cell(row, c_ceo),
                     "job_title": cell(row, c_job),
                     "size_blank": not re.search(r"\d", cell(row, c_size)),
                     "band": size_band(cell(row, c_size))})

    # Spend credits on small orgs first (Jude, 2026-07-31). Measured reply rate
    # by size on Indiana + Texas: TINY <50 3.52%, size-unknown 2.74%, MID
    # 50-499 1.20%, LARGE 500+ 0.00% (0/244). Blank size sorts with TINY —
    # size_band() already maps it there, and unknown-size rows reply at 2.74%.
    # Sort is stable, so sheet order is preserved inside each band. This
    # reorders WHO gets the limited budget; it never drops a row (use
    # --skip_large for that).
    total_eligible = len(todo)
    if args.skip_large:
        todo = [t for t in todo if t["band"] != "LARGE"]
    if args.size_priority:
        todo.sort(key=lambda t: {"TINY": 0, "MID": 1, "LARGE": 2}[t["band"]])
    todo = todo[:args.limit]

    from collections import Counter
    bands = Counter(t["band"] for t in todo)
    order = "small-first" if args.size_priority else "sheet order"
    print(f"=== Apollo DM Waterfall === {len(todo)} of {total_eligible} eligible "
          f"rows to process (limit {args.limit}, {order}"
          + (", LARGE skipped" if args.skip_large else "") + "; " +
          ", ".join(f"{bands.get(b, 0)} {b}" for b in ("TINY", "MID", "LARGE")) + ")")
    if args.dry_run:
        for t in todo:
            print(f"  [DRY] row {t['sheet_row']} [{t['band']}]: {t['company']} ({t['domain']})")
        return

    stats = {"emails": 0, "amf_credits": 0, "google_calls": 0,
             "apollo_empty": 0, "no_candidates": 0, "not_found": 0}
    lock = threading.Lock()
    cols = (c_dm, c_dmt, c_li, c_email, c_first, c_last, c_status, args.tab,
            c_size)

    done = 0
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=CHUNK_WORKERS) as ex:
            results = list(ex.map(
                lambda t: process_lead(t, llm, cols, stats, lock), chunk))
        pending = [u for updates in results for u in updates]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW",
                  "data": [{"range": r, "values": [v]} for r, v in pending]},
        ).execute()
        done += len(chunk)
        label = "DMs so far" if SKIP_EMAIL else "emails so far"
        count = stats.get("dm_only", 0) if SKIP_EMAIL else stats["emails"]
        print(f"  -- batch written ({done}/{len(todo)}) | {label}: {count}")

    print("\n=== Summary ===")
    if SKIP_EMAIL:
        print(f"  DMs identified:    {stats.get('dm_only', 0)}/{len(todo)} "
              f"(identity only — no emails, 0 AMF credits)")
    else:
        print(f"  Verified emails:   {stats['emails']}/{len(todo)}")
    print(f"  AMF credits spent: {stats['amf_credits']}")
    print(f"  Google queries:    {stats['google_calls']}")
    print(f"  Apollo empty:      {stats['apollo_empty']}")
    print(f"  No DM candidates:  {stats['no_candidates']}")
    print(f"  Ranked but no email: {stats['not_found']}")


if __name__ == "__main__":
    main()
