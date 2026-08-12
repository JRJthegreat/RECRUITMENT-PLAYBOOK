"""
Resolve a company's official website domain via Exa, and only write it when the
PAGE ITSELF proves the match.

WHY THIS EXISTS
---------------
Two cheaper approaches were measured on the Florida healthcare sheet and both
failed:

  * Guessing domains from the company name and checking the page mentions the
    name — 25% recall, ~50% precision. Structurally circular: a page at
    lakenona.com mentions "Lake Nona" whether it is the clinic or the property
    developer. It produced orlando.org for Orlando Health, lhc.org (a church in
    Austin) for The Lakes Home Care, precision.com (a data-products firm) for
    Precision Healthcare Specialists.
  * Google-via-Apify + an LLM picking among results — better, but it silently
    returned parent-chain and careers domains: 28 HCA facilities all resolved to
    careers.hcahealthcare.com and 15 Life Care homes to lcca.com, which makes
    Apollo hand back the SAME corporate person for every one of them.

ACCEPTANCE (rebuilt 2026-08-10, Jude's calls: mirror find_company_domains.py's
judgment structure, and the judge is CLAUDE IN THE SESSION — no GPT, no
per-call LLM API spend)
-----------------------------------------------------------
The first version of this script judged mechanically: prefix-in-domain fast
path, then token+location content matching. In practice that produced false
positives and false negatives whenever the domain didn't literally resemble
the company name (or resembled a namesake's). The decision structure now
mirrors Jude's tested resolver (healthcare-demand-pipeline/
find_company_domains.py, feeding the LIVE Indiana pipeline): mechanical junk
filtering, then a model reads each candidate (domain + title + page text)
and picks the official site or nothing. The model is the Claude Code session
itself, via a two-step flow:

  1. COLLECT (this script, --apply): Exa search per row -> mechanical
     prefilter -> candidates JSON. Rows with zero candidates are stamped
     exa_no_match immediately.
  2. JUDGE (Claude, in-session): read the candidates file, write verdicts
     JSON [{"row": N, "domain": "x.com" | ""}].
  3. APPLY (this script, --verdicts FILE --apply): validate each verdict is
     one of that row's candidates, enforce the parent-chain guard, write
     website + status. A verdict outside the candidate set is refused.

What is deliberately KEPT from this script's own history are the mechanical
guards that fix the LLM-pick failure modes measured above:
  * careers./jobs. subdomains and job-page URLs rejected before judging
  * the parent-chain guard — a domain already claimed by a DIFFERENT company
    on the same sheet is never reused (the 28-HCA-rows trap)
  * the junk-host list (directories, registries, portfolio hosts)

Statuses: ok_claude (written) / no_match / verdict_refused / reject classes.

REJECTED OUTRIGHT (never written, with the reason recorded)
-----------------------------------------------------------
  directories/aggregators, job boards, social, site builders, government portals,
  `careers.`/`jobs.` subdomains, and any domain already claimed by a DIFFERENT
  company on the same sheet (the parent-chain trap).

A blank from an error must never look like a blank from "this company has no
website" — the status column distinguishes them.

COST
----
Exa search is ~$0.007 per company (measured). The script prints the estimate
and asks for --apply before spending anything. A 402 means out of credits: it
stops immediately rather than burning the rest of the run.

USAGE
-----
  # step 1 — collect candidates (the only step that spends Exa credits)
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" --limit 20              # dry run
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" --apply \
      --candidates exa_candidates.json

  # step 2 — Claude judges exa_candidates.json in-session -> verdicts.json

  # step 3 — apply verdicts (no Exa spend)
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" \
      --candidates exa_candidates.json --verdicts verdicts.json --apply

  --col_company K --col_website L --col_city R --col_state S --col_status AB
  --overwrite     also re-resolve rows that already have a website
  --niche "healthcare practice"   words added to the query (default: none)
"""
import os
import re
import sys
import json
import time
import argparse
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", "..", ".env"))
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")

EXA_URL = "https://api.exa.ai/search"
EXA_COST_PER_SEARCH = 0.007          # measured; used only for the estimate
BATCH = 10                            # sheet write cadence (crash-safe)

# Hosts that are never a company's own site. Matched on the HOST, anchored —
# an earlier substring version flagged newsmyrnawellness.com as a "wellness.com
# directory", which is a real practice.
JUNK_HOSTS = (
    "indeed.com", "ziprecruiter.com", "glassdoor.com", "linkedin.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "yelp.com", "healthgrades.com", "zocdoc.com", "vitals.com", "wellness.com",
    "webmd.com", "sharecare.com", "ratemds.com", "caredash.com", "doximity.com",
    "mapquest.com", "yellowpages.com", "bbb.org", "manta.com", "bizapedia.com",
    "dnb.com", "crunchbase.com", "bloomberg.com", "zoominfo.com", "apollo.io",
    "g.page", "sites.google.com", "business.site", "wixsite.com",
    "squarespace.com", "godaddysites.com", "wordpress.com", "weebly.com",
    "medium.com", "wikipedia.org", "usnews.com", "medicare.gov", "cms.gov",
    "nih.gov", "ncbi.nlm.nih.gov", "simplyhired.com", "monster.com",
    "careerbuilder.com", "snagajob.com", "jobs.net", "talent.com", "lensa.com",
    "salary.com", "payscale.com", "trustpilot.com", "angi.com", "thumbtack.com",
    "wayup.com", "jobcase.com", "adzuna.com", "jooble.org", "workable.com",
    "greenhouse.io", "lever.co", "smartrecruiters.com", "icims.com", "myworkdayjobs.com",
    "paylocity.com", "bamboohr.com", "jazzhr.com", "breezy.hr", "recruiterbox.com",
    "sunbiz.org", "opencorporates.com", "buzzfile.com", "corporationwiki.com",
    "opengovus.com", "bizprofile.net", "companiesinfo.com", "dandb.com",
    "healthfinder.fl.gov", "npidb.org", "npino.com", "hipaaspace.com",
    # NPI-registry mirrors. These are the highest-risk false positive on a
    # list sourced FROM the NPI registry: the page names the practice, the
    # city and the taxonomy, so it satisfies content verification perfectly
    # while being a directory. Measured: 12 of 789 resolved domains (1.5%)
    # on the first healthcare run, all from this family.
    "opennpi.com", "npiprofile.com", "npino.org", "npiindex.com", "npiscan.com",
    "npidataservices.com", "npitelehealth.com", "npi-lookup.org", "npilookup.com",
    "npiregistry.cms.hhs.gov", "healthcare4ppl.com", "medicarelist.com",
    "medicaidspending.org", "doccafe.com", "opencorpdata.com", "psychologytoday.com",
    "healthcaresix.com", "npidb.com", "npies.com", "findanpi.com",
    # Senior-care and home-care referral directories. These dominate organic
    # results for agency names, and a match here reads as a perfect content
    # verification (the page names the agency AND its city) while belonging to
    # a lead-gen company. Live failure: CAREGIVERS ON DEMAND LLC resolved to
    # aplaceformom.com, and enrichment then returned that directory's CEO.
    "aplaceformom.com", "caring.com", "seniorly.com", "carelistings.com",
    "senioradvisor.com", "agingcare.com", "care.com", "seniorcare.com",
    "assistedliving.com", "seniorliving.com", "homecare.com", "carepathways.com",
    "medicare.com", "eldercarelink.com", "afterschoolhq.com",
    # Generic business-profile aggregators
    "bisprofiles.com", "bizstanding.com", "companytrace.com", "govtribe.com",
    "opengovwin.com", "usaspending.gov", "cortera.com", "zippia.com",
    "rocketreach.co", "leadiq.com", "signalhire.com", "lusha.com",
    # Production/creative-vertical directories and portfolio hosts (added for
    # the production-house lane). productionhub.com is the NPI-mirror trap of
    # this vertical: the lead list is sourced FROM it, and its profile pages
    # name the company AND its city. Vimeo/Behance are portfolio hosts every
    # studio has — never their own site.
    "productionhub.com", "mandy.com", "staffmeup.com", "imdb.com",
    "vimeo.com", "behance.net", "peerspace.com", "giggster.com",
    "clutch.co", "sortlist.com", "expertise.com", "upcity.com",
    "designrush.com", "themanifest.com", "productionbeast.com",
    # Local-listing aggregators and content-farm blogs — surfaced by Google
    # search (Apify collector) rather than Exa; caught live on the first
    # Apify test batch (local.yahoo.com, a Yahoo Local business listing;
    # feedspot.com, an unrelated podcast-roundup blog).
    "local.yahoo.com", "yahoo.com", "feedspot.com",
)

# Secretary-of-State registry mirrors publish one page per registered entity
# with the legal name and address, which also passes content verification.
# They are a family, not a fixed list: ohio-corp.com, colorado-corp.com, etc.
JUNK_HOST_RE = re.compile(
    r"(^|\.)([a-z]+-corp|corp-[a-z]+)\.com$|"
    r"(^|\.)(bizfile|sos)\.|"
    r"\.birdeye\.com$|"
    r"(^|\.)(opencorporates|corporationwiki|bizapedia)\.", re.I)

# A job-posting page always names the company and its city — which is exactly the
# content signal this script trusts. On a lead list sourced FROM job ads, that
# makes job boards the most likely false positive of all, and they cannot all be
# enumerated by hostname (wayup.com slipped through a 50-host blocklist). So
# detect the PAGE SHAPE instead.
JOB_PATH = re.compile(r"/(jobs?|careers?|apply|vacanc|opening|hiring|employment)[/\-_.]?", re.I)
JOB_TEXT = re.compile(r"apply now|easy apply|job description|job type|apply for this job|"
                      r"view all jobs|similar jobs|job alert|posted \d+ (day|hour|week)|"
                      r"full[- ]time|part[- ]time|salary range|estimated salary|"
                      r"we are hiring|now hiring|join our team and apply", re.I)


def looks_like_job_page(url, blob):
    if JOB_PATH.search(url or ""):
        return True
    # one stray "full-time" on a real practice page is not proof; three markers is
    return len(set(JOB_TEXT.findall(blob or ""))) >= 3
CAREERS_SUB = re.compile(r"^(careers?|jobs|apply|talent|recruit|workforce|hiring)\.", re.I)

LEGAL = re.compile(r"\b(inc|llc|pllc|p\.?c\.?|p\.?a\.?|llp|ltd|corp|corporation|co)\b\.?", re.I)


def norm_domain(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0].strip()


def is_junk(host):
    h = host.lower()
    if CAREERS_SUB.match(h):
        return "careers_subdomain"
    for d in JUNK_HOSTS:
        if h == d or h.endswith("." + d):
            return "directory_or_jobboard"
    if JUNK_HOST_RE.search(h):
        return "registry_mirror"
    if h.endswith(".gov"):
        return "government"
    return None




def exa_search(company, city, state, niche, key, n=6):
    """Return (results, cost, error). Results carry url/title/text."""
    q = " ".join(x for x in [company, niche, city, state, "official website"] if x)
    body = {
        "query": q,
        "numResults": n,
        "type": "auto",
        # Ask for page text in the SAME call — content verification is the whole
        # point, and a separate /contents request would double the cost.
        "contents": {"text": {"maxCharacters": 1200}},
    }
    # 429 is a rate limit, not a failure — it means we asked too fast, and the
    # answer is still there. Retrying with backoff recovers rows that would
    # otherwise look identical to "this company has no website".
    r = None
    for attempt in range(4):
        try:
            r = requests.post(EXA_URL, headers={"x-api-key": key,
                                                "Content-Type": "application/json"},
                              json=body, timeout=45)
        except requests.RequestException as e:
            if attempt == 3:
                return [], 0.0, f"error:{type(e).__name__}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep((2 ** attempt) + 1)
            continue
        break
    if r is None:
        return [], 0.0, "error:no_response"
    if r.status_code == 429:
        return [], 0.0, "error:http_429_after_retries"
    # 402/401 used to raise SystemExit here to stop the run immediately. That
    # broke silently: exa_search runs inside a ThreadPoolExecutor worker, and
    # main() never consumes pool.map()'s return value, so an exception raised
    # in a worker thread is captured on its Future and discarded — never
    # printed, never re-raised. Every row in the batch failed identically
    # (same key, same credit balance) and the run reported "$0.000 spent, 0
    # rows" with no error at all. Returning an error tuple instead lets
    # work() record it in `stats`, and main() checks for exactly these two
    # codes after the pool finishes to print a loud, unmissable message.
    if r.status_code == 402:
        return [], 0.0, "error:out_of_credits"
    if r.status_code == 401:
        return [], 0.0, "error:invalid_api_key"
    if r.status_code != 200:
        return [], 0.0, f"error:http_{r.status_code}"
    d = r.json()
    return d.get("results", []), (d.get("costDollars") or {}).get("total", 0.0) or 0.0, None


# ---- Candidate collection for Claude-in-session judging -------------------
# Jude's call (2026-08-10): no GPT. The judge is Claude itself, in the Claude
# Code session, reading the collected candidates and returning verdicts. The
# decision structure still mirrors find_company_domains.py — junk filtered
# mechanically, then a model reads domain + title + page text and picks the
# official site or nothing — but the model is the session, not an API call.
#
# Judging rules (for the session doing the verdicts):
#   - Pick the company's own corporate/brand website, not third-party listings.
#   - The domain does NOT have to contain the company name: sister brands,
#     acronyms and service+city domains are fine IF the page text clearly
#     names the company.
#   - Name collision or wrong-location namesake -> "" (skip).
#   - Not clearly the official site -> "" — a blank beats a wrong domain.
#
# Verdicts file format (JSON): [{"row": 2, "domain": "example.com"}, ...]
# with "" as the domain for NONE. A verdict domain that was not among that
# row's collected candidates is refused at apply time.


def build_candidates(company, results, taken):
    """Mechanically prefilter Exa results into judgeable candidates."""
    candidates, seen = [], set()
    for item in results:
        dom = norm_domain(item.get("url", ""))
        if not dom or "." not in dom or dom in seen:
            continue
        if is_junk(dom):
            continue
        url_full = item.get("url", "")
        blob_raw = ((item.get("title") or "") + " " + (item.get("text") or ""))
        if looks_like_job_page(url_full, blob_raw):
            continue
        # The parent-chain trap: a domain already proven to belong to a
        # DIFFERENT company on this sheet must never be reused. This is what
        # produced 28 HCA rows and 15 Life Care rows pointing at one domain.
        owner = taken.get(dom)
        if owner and owner.lower() != (company or "").lower():
            continue
        seen.add(dom)
        candidates.append({"domain": dom,
                           "title": (item.get("title") or "")[:150],
                           "text": (item.get("text") or "")[:300]})
    return candidates


def col_idx(letter):
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def idx_col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--col_company", default="K")
    ap.add_argument("--col_website", default="L")
    ap.add_argument("--col_city", default="R")
    ap.add_argument("--col_state", default="S")
    ap.add_argument("--col_status", default="")
    ap.add_argument("--niche", default="", help='e.g. "healthcare practice"')
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--retry_attempted", action="store_true",
                    help="re-search rows whose status shows a prior exa attempt "
                         "(default: skip them so --limit reaches FRESH rows)")
    ap.add_argument("--candidates", default="exa_candidates.json",
                    help="where the collect step writes candidates for Claude to judge")
    ap.add_argument("--verdicts", default=None,
                    help="apply mode: JSON file of Claude's verdicts "
                         '[{"row": N, "domain": "x.com" | ""}] — no Exa spend')
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    key = os.getenv("EXA_API_KEY")
    if not key and not args.verdicts:
        sys.exit("EXA_API_KEY not set. Add it to .claude/.env")

    sid = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", args.sheet_url).group(1)
    svc = build("sheets", "v4",
                credentials=Credentials.from_authorized_user_file(TOKEN_PATH))
    vals = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{args.tab}!A1:BZ20000").execute().get("values", [])
    rows = vals[1:]
    C, W = col_idx(args.col_company), col_idx(args.col_website)
    CY, ST = col_idx(args.col_city), col_idx(args.col_state)
    STA = col_idx(args.col_status) if args.col_status else None

    def cell(r, i):
        return (r[i] if i is not None and len(r) > i else "").strip()

    # Domains already proven to belong to a specific company — used to block the
    # parent-chain reuse described above.
    taken = {}
    for r in rows:
        d = norm_domain(cell(r, W))
        if d and "." in d:
            taken.setdefault(d, cell(r, C))

    # ---------------- apply mode: write Claude's verdicts, no Exa spend ----
    if args.verdicts:
        with open(args.candidates) as f:
            collected = {c["row"]: c for c in json.load(f)}
        with open(args.verdicts) as f:
            verdicts = json.load(f)

        stats = Counter()
        writes = []
        for v in verdicts:
            row, dom = v["row"], norm_domain(v.get("domain") or "")
            rec = collected.get(row)
            if rec is None:
                print(f"  [!] row {row}: no collected candidates — skipped")
                continue
            status = "no_match"
            if dom:
                allowed = {c["domain"] for c in rec["candidates"]}
                if dom not in allowed:
                    print(f'  [!] row {row} ({rec["company"]}): verdict {dom} is not '
                          f"among its candidates — refused")
                    dom, status = "", "verdict_refused"
                elif dom in taken and taken[dom].lower() != rec["company"].lower():
                    dom, status = "", "domain_claimed_by_other_company"
                else:
                    taken[dom] = rec["company"]
                    status = "ok_claude"
            if dom:
                writes.append({"range": f"{args.tab}!{idx_col(W)}{row}",
                               "values": [[dom]]})
            if STA is not None:
                writes.append({"range": f"{args.tab}!{idx_col(STA)}{row}",
                               "values": [[f"exa_{status}"]]})
            stats[status] += 1

        if not args.apply:
            print(f"[DRY RUN] would write {len(writes)} cells: {dict(stats)}. "
                  "Re-run with --apply.")
            return
        for k in range(0, len(writes), 100):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": writes[k:k + 100]}).execute()
        print(f"=== Verdicts applied ===")
        for k, n in stats.most_common():
            print(f"  {k:36} {n}")
        return

    # ---------------- collect mode: Exa search + prefilter -> candidates ----
    todo = []
    for i, r in enumerate(rows):
        name = cell(r, C)
        if not name:
            continue
        if cell(r, W) and "." in cell(r, W) and not args.overwrite:
            continue
        # A prior attempt that ended no_match leaves the website cell BLANK
        # but stamps the status column — without this check those rows sit at
        # the top of the sheet and get re-searched (and re-billed) on every
        # --limit run before any fresh row is reached. Re-attempt them only
        # with --retry_attempted.
        if (STA is not None and cell(r, STA).startswith("exa_")
                and not args.overwrite and not args.retry_attempted):
            continue
        todo.append({"row": i + 2, "company": name,
                     "city": cell(r, CY), "state": cell(r, ST)})
    if args.limit:
        todo = todo[:args.limit]

    print(f"=== Exa candidate collection {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
    print(f"rows needing a website: {len(todo)}")
    print(f"estimated Exa cost:     ~${len(todo) * EXA_COST_PER_SEARCH:.2f}")
    if not args.apply:
        print("\n[DRY RUN] no Exa calls, nothing spent, nothing written. "
              "Re-run with --apply.")
        for t in todo[:15]:
            print(f"  would resolve: {t['company']}  ({t['city']}, {t['state']})")
        return

    lock = threading.Lock()
    spent = [0.0]
    stats = Counter()
    errors_out = []       # (t, err) — stamped on the sheet immediately
    collected_out = []    # rows with candidates — for Claude to judge

    def work(t):
        try:
            res, cost, err = exa_search(t["company"], t["city"], t["state"],
                                        args.niche, key)
        except Exception as e:
            # Belt-and-suspenders: work() runs inside a thread pool whose
            # results are force-consumed below specifically so a bug like
            # this can never again vanish silently (see the 402/401 note in
            # exa_search). Any exception that still gets here is recorded,
            # not swallowed.
            with lock:
                stats[f"error:{type(e).__name__}"] += 1
                errors_out.append((t, f"error:{type(e).__name__}"))
            return
        with lock:
            spent[0] += cost
            if err:
                stats[err] += 1
                errors_out.append((t, err))
                return
            cands = build_candidates(t["company"], res, taken)
            if not cands:
                stats["no_candidates"] += 1
                errors_out.append((t, "no_match"))
            else:
                stats["collected"] += 1
                collected_out.append({**t, "candidates": cands})

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # list(...) forces the pool to actually iterate its own map() result.
        # Without this, exceptions raised inside work() are captured on their
        # Future and never observed — see the comment above exa_search's
        # 402/401 handling for what that cost us.
        list(pool.map(work, todo))

    fatal = stats.get("error:out_of_credits", 0) + stats.get("error:invalid_api_key", 0)
    if fatal and fatal == len(todo):
        reason = "out of Exa credits" if "error:out_of_credits" in stats else "Exa API key rejected"
        print(f"\n!!! STOPPED: every one of {len(todo)} rows failed — {reason}. "
              f"Nothing was found because nothing could be searched.")
        if "error:out_of_credits" in stats:
            print("    Top up at dashboard.exa.ai, then re-run this exact command — "
                  "nothing was written to the sheet, so no rows were skipped.")
        else:
            print("    Check EXA_API_KEY in .claude/.env, then re-run this exact command.")
        return
    elif fatal:
        print(f"\n!!! WARNING: {fatal}/{len(todo)} rows failed on Exa credits/auth mid-run — "
              f"top up and re-run with --retry_attempted to pick them back up.")

    # Rows with nothing to judge get their status stamped now; rows with
    # candidates are NOT touched until the verdicts pass.
    if STA is not None and errors_out:
        writes = [{"range": f"{args.tab}!{idx_col(STA)}{t['row']}",
                   "values": [[f"exa_{err}"]]} for t, err in errors_out]
        for k in range(0, len(writes), 100):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "RAW", "data": writes[k:k + 100]}).execute()

    collected_out.sort(key=lambda c: c["row"])
    with open(args.candidates, "w") as f:
        json.dump(collected_out, f, indent=1)

    print(f"\n=== Summary ===")
    print(f"  Exa spend:      ${spent[0]:.3f}")
    for k, n in stats.most_common():
        print(f"    {k:36} {n}")
    print(f"\n{len(collected_out)} rows with candidates -> {args.candidates}")
    print("Next: have Claude read that file, write verdicts JSON "
          '([{"row": N, "domain": "x.com" | ""}]), then re-run with '
          f"--verdicts VERDICTS_FILE --candidates {args.candidates} --apply")


if __name__ == "__main__":
    main()
