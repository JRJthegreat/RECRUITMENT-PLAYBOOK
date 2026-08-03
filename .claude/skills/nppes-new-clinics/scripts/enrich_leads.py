"""
Phase 3.5 — enrich leads to a verified owner email. COSTS MONEY.

NPPES already hands us the decision maker: the Authorized Official is the
owner/CEO/founder on 65% of records, and owners reply 4.2x everything else
(measured on 1,209 leads). So there is no Apollo waterfall and no DM-ranking
step here. Two stages only:

  1. DOMAIN   Apify Google search + GPT-4.1 pick   ~$0.007 per company
  2. EMAIL    AnyMail Finder /find-email/person    1 credit per FOUND valid
              email (owner first+last + domain)

Only `valid` emails are written. Rows that fail either stage keep their phone
and stay in the store for a later retry (a practice with no website today
often has one in 60 days).

Defaults are deliberately conservative:
  --dry_run          print what would be looked up, spend nothing (DEFAULT OFF
                     but always run it first)
  --owners_only      skip authorized officials whose title is not owner-like
                     (default ON — per the owner-first reply data)
  --backed_first     order by expansion rows first (existing parent business =
                     a website already exists = far better domain hit rate)

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/enrich_leads.py \
      --category home_health_hospice --limit 50 --dry_run
  ... then drop --dry_run to spend.
"""
import argparse
import os
import re
import sys
import time
from datetime import date

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import get_db, log_run

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR = "apify~google-search-scraper"
APIFY_BASE = "https://api.apify.com/v2"
AMF_KEY = os.getenv("ANYMAILFINDER_API_KEY")
AMF_PERSON_URL = "https://api.anymailfinder.com/v5.0/search/person.json"
AZ_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZ_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZ_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZ_FAST = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST")

OWNER_TITLE_RE = re.compile(
    r"OWNER|CEO|FOUNDER|PRESIDENT|PRINCIPAL|MANAGING (MEMBER|PARTNER|DIRECTOR)"
    r"|MEDICAL DIRECTOR|PARTNER|PROPRIETOR|CHIEF EXECUTIVE", re.I)

BLOCKED_HOSTS = (
    "indeed.", "linkedin.", "facebook.", "instagram.", "twitter.", "x.com",
    "yelp.", "glassdoor.", "ziprecruiter.", "npidb.", "npino.", "npiprofile.",
    "healthgrades.", "zocdoc.", "wellness.com", "bbb.org", "mapquest.",
    "yellowpages.", "manta.", "bizapedia.", "opencorporates.", "buzzfile.",
    "crunchbase.", "dnb.com", "apollo.io", "zoominfo.", "rocketreach.",
    "medicare.gov", "cms.gov", "caredash.", "vitals.com", "sharecare.",
    "doximity.", "webmd.", "chamberofcommerce.com", "birdeye.", "nppes.",
)
LEGAL_SUFFIX_RE = re.compile(
    r"[,\s]+(l\.?l\.?c|p\.?l\.?l\.?c|inc|incorporated|corp|corporation|"
    r"p\.?c|p\.?a|l\.?l\.?p|l\.?p|ltd|limited|co)\.?$", re.I)


def clean_name(name):
    out = LEGAL_SUFFIX_RE.sub("", (name or "").strip()).strip(" ,.")
    return LEGAL_SUFFIX_RE.sub("", out).strip(" ,.")


def root_domain(url):
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else url or "").lower().lstrip("www.")
    return host.split("/")[0].strip()


def apify_google(queries):
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json={"queries": "\n".join(queries), "resultsPerPage": 5,
              "maxPagesPerQuery": 1, "languageCode": "en",
              "countryCode": "us", "includeUnfilteredResults": False},
        timeout=300)
    if resp.status_code not in (200, 201):
        print(f"  [!] Apify HTTP {resp.status_code}: {resp.text[:160]}")
        return {}
    out = {}
    for item in resp.json():
        q = (item.get("searchQuery") or {}).get("term", "")
        if q:
            out[q] = item.get("organicResults", [])
    return out


def llm_pick_domain(name, city, state, candidates):
    """GPT-4.1 picks the official site, or NONE. Clinic names collide hard,
    so a wrong pick is worse than no pick."""
    listing = "\n".join(f"{i+1}. {c['domain']} — {c['title'][:80]}"
                        for i, c in enumerate(candidates))
    prompt = (
        f"Practice: {name}\nLocation: {city}, {state}\n\nCandidate sites:\n{listing}\n\n"
        "Which is the practice's own official website? Healthcare names collide "
        "constantly, so if none clearly belongs to THIS practice in THIS city, "
        "answer NONE. Reply with only the domain or NONE.")
    try:
        r = requests.post(
            f"{AZ_ENDPOINT}/openai/deployments/{AZ_FAST}/chat/completions",
            params={"api-version": AZ_VERSION},
            headers={"api-key": AZ_KEY, "Content-Type": "application/json"},
            json={"messages": [
                {"role": "system", "content": "You match businesses to their official website. Answer with a bare domain or NONE."},
                {"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": 24},
            timeout=60)
        if r.status_code != 200:
            return ""
        ans = r.json()["choices"][0]["message"]["content"].strip().lower()
        ans = root_domain(ans)
        return "" if "none" in ans or not ans else ans
    except Exception:
        return ""


def find_domain(rows_batch):
    """One Apify call for up to 10 practices -> {npi: domain}."""
    queries, qmap = [], {}
    for p in rows_batch:
        q = f'"{clean_name(p["org_name"])}" {p["city"]} {p["state"]}'
        queries.append(q)
        qmap[q] = p
    results = apify_google(queries)
    found = {}
    for q, organic in results.items():
        p = qmap.get(q)
        if not p:
            continue
        cands = []
        for o in organic:
            d = root_domain(o.get("url", ""))
            if not d or any(b in d for b in BLOCKED_HOSTS) or d.endswith((".gov", ".mil")):
                continue
            if d not in [c["domain"] for c in cands]:
                cands.append({"domain": d, "title": o.get("title", "")})
        if cands:
            pick = llm_pick_domain(p["org_name"], p["city"], p["state"], cands)
            if pick:
                found[p["npi"]] = pick
    return found


def find_email(first, last, domain, company):
    if not (first and last and domain):
        return None, "missing_data"
    try:
        r = requests.post(
            AMF_PERSON_URL,
            headers={"Authorization": AMF_KEY, "Content-Type": "application/json"},
            json={"full_name": f"{first} {last}".strip(), "first_name": first,
                  "last_name": last, "domain": domain, "company_name": company},
            timeout=180)
        if r.status_code == 402:
            return None, "no_credits"
        r.raise_for_status()
        d = r.json()
        email, status = d.get("email"), d.get("email_status", "unknown")
        # valid only — risky is rejected everywhere in this repo
        return (email, "valid") if email and status == "valid" else (None, status or "not_found")
    except requests.exceptions.HTTPError as e:
        return None, f"http_{e.response.status_code}"
    except Exception:
        return None, "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=None, help="taxonomy_category filter")
    ap.add_argument("--taxonomy_code", default=None)
    ap.add_argument("--states", default=None)
    ap.add_argument("--classification", default="NEW_INDEPENDENT,NEW_LOCATION,HEALTH_SYSTEM_EXPANSION")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min_score", type=int, default=None)
    ap.add_argument("--owners_only", dest="owners_only", action="store_true", default=True)
    ap.add_argument("--all_titles", dest="owners_only", action="store_false",
                    help="include non-owner authorized officials (worse reply rate)")
    ap.add_argument("--backed_first", action="store_true",
                    help="expansion rows first — parent business already has a website")
    ap.add_argument("--retry_failed", action="store_true",
                    help="re-attempt rows whose previous enrichment failed")
    ap.add_argument("--per_site", dest="dedupe_owner", action="store_false", default=True,
                    help="enrich every site row (default: one row per owner)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    for name, val in [("APIFY_API_TOKEN", APIFY_TOKEN), ("ANYMAILFINDER_API_KEY", AMF_KEY),
                      ("AZURE_OPENAI_ENDPOINT", AZ_ENDPOINT)]:
        if not val and not args.dry_run:
            sys.exit(f"[enrich] missing {name} in .claude/.env")

    conn = get_db()
    cls = args.classification.split(",")
    clauses = [f"classification IN ({','.join('?' * len(cls))})",
               "(solo_flag IS NULL OR solo_flag = 0)",
               "TRIM(COALESCE(ao_first,'')) != ''", "TRIM(COALESCE(ao_last,'')) != ''"]
    params = list(cls)
    clauses.append("email IS NULL" if args.retry_failed else
                   "email IS NULL AND email_source IS NULL")
    if args.category:
        clauses.append("taxonomy_category = ?"); params.append(args.category)
    if args.taxonomy_code:
        clauses.append("taxonomy_code = ?"); params.append(args.taxonomy_code)
    if args.states:
        st = args.states.split(","); clauses.append(f"state IN ({','.join('?' * len(st))})"); params += st
    if args.min_score is not None:
        clauses.append("score >= ?"); params.append(args.min_score)

    order = ("CASE WHEN classification='NEW_INDEPENDENT' THEN 1 ELSE 0 END, score DESC"
             if args.backed_first else "score DESC")
    rows = conn.execute(
        f"SELECT * FROM practices WHERE {' AND '.join(clauses)} ORDER BY {order}",
        params).fetchall()
    if args.owners_only:
        rows = [r for r in rows if OWNER_TITLE_RE.search(r["ao_title"] or "")]

    # One row per OWNER, not per site. Multi-site operators file a separate NPI
    # per location (one franchisee had 3 in this pool), so without this we pay
    # to enrich the same person repeatedly and then email them 3x.
    n_before = len(rows)
    if args.dedupe_owner:
        seen, deduped = set(), []
        for r in rows:                       # rows are already score-ordered
            key = (r["ao_first"].strip().upper(), r["ao_last"].strip().upper())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        rows = deduped
    n_dupes = n_before - len(rows)
    rows = rows[:args.limit]

    est = len(rows) * 0.007
    print(f"[enrich] {len(rows)} leads"
          f"{f' ({n_dupes} extra sites collapsed into their owner)' if n_dupes else ''}"
          f" | est. Apify ~${est:.2f} + up to {len(rows)} AMF credits "
          f"(charged only on found valid emails)"
          f"{' | DRY RUN — no spend' if args.dry_run else ''}")
    if not rows:
        return
    if args.dry_run:
        for p in rows[:25]:
            print(f"  {p['npi']} {p['org_name'][:40]:40s} {p['city'][:14]:14s} {p['state']} "
                  f"| {p['ao_first']} {p['ao_last']} ({p['ao_title'][:22]})")
        print("[enrich] DRY RUN — nothing looked up, nothing spent")
        return

    n_dom = n_email = 0
    stamp = date.today().isoformat()
    for i in range(0, len(rows), 10):          # batch-of-10
        batch = rows[i:i + 10]
        domains = find_domain(batch)
        n_dom += len(domains)
        for p in batch:
            dom = domains.get(p["npi"])
            if not dom:
                conn.execute("UPDATE practices SET email_source=?, email_verified=? WHERE npi=?",
                             ("no_domain", stamp, p["npi"]))
                continue
            email, status = find_email(p["ao_first"], p["ao_last"], dom, p["org_name"])
            if email:
                n_email += 1
            conn.execute(
                "UPDATE practices SET domain=?, email=?, email_source=?, email_verified=? WHERE npi=?",
                (dom, email, f"amf_person:{status}", stamp, p["npi"]))
            if status == "no_credits":
                print("  [!] AnyMail Finder out of credits — stopping")
                conn.commit()
                return
            time.sleep(0.2)
        conn.commit()
        print(f"  {min(i+10, len(rows))}/{len(rows)} | domains {n_dom} | emails {n_email}", end="\r")

    summary = (f"{len(rows)} leads -> {n_dom} domains ({100*n_dom/len(rows):.0f}%) "
               f"-> {n_email} valid owner emails ({100*n_email/len(rows):.0f}%)")
    print(f"\n[enrich] {summary}")
    log_run(conn, "enrich_leads", summary)
    conn.close()


if __name__ == "__main__":
    main()
