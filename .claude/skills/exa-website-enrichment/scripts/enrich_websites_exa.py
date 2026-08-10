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

ACCEPTANCE (rebuilt 2026-08-10, Jude's call: mirror find_company_domains.py)
-----------------------------------------------------------
The first version of this script judged mechanically: prefix-in-domain fast
path, then token+location content matching. In practice that produced false
positives and false negatives whenever the domain didn't literally resemble
the company name (or resembled a namesake's). Jude's tested resolver —
healthcare-demand-pipeline/find_company_domains.py, which feeds the LIVE
Indiana pipeline — decides differently: mechanical junk filtering, then
**GPT-4.1 reads each candidate (domain + title + page text) and picks the
official site or NONE**. This script now mirrors that decision structure,
with Exa as the search layer (its 1,200-char page text is richer evidence
than Google snippets).

What is deliberately KEPT from this script's own history are the mechanical
guards that fix the LLM-pick failure modes measured above:
  * careers./jobs. subdomains and job-page URLs rejected before judging
  * the parent-chain guard — a domain already claimed by a DIFFERENT company
    on the same sheet is never reused (the 28-HCA-rows trap)
  * the junk-host list (directories, registries, portfolio hosts)

Statuses: ok_llm (written) / no_match (LLM declined) / the reject classes.

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
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" --limit 20          # dry run
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" --limit 20 --apply
  python3 -W ignore enrich_websites_exa.py --sheet_url "URL" --apply             # all blanks

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
    if r.status_code == 402:
        raise SystemExit("Exa is out of credits (402). Top up at dashboard.exa.ai — "
                         "stopping now rather than burning the rest of the run.")
    if r.status_code == 401:
        raise SystemExit("Exa rejected the API key (401). Check EXA_API_KEY in .claude/.env")
    if r.status_code != 200:
        return [], 0.0, f"error:http_{r.status_code}"
    d = r.json()
    return d.get("results", []), (d.get("costDollars") or {}).get("total", 0.0) or 0.0, None


# ---- LLM pick, mirroring find_company_domains.py -------------------------
# Same decision structure, same conservative NONE-leaning rules, same
# answer-must-be-a-candidate constraint. Differences: candidates carry Exa's
# page-text excerpt (richer than a Google snippet), and the model is told the
# company's location so namesakes in other cities get NONE'd.

AZ_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZ_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZ_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

LLM_SYSTEM = (
    "You identify a company's official website from web search results. "
    "You will receive a company name, its location, and numbered candidate "
    "results (domain + title + page text excerpt). Reply with ONLY the bare "
    "domain of the official site (e.g. 'example.com') or the word NONE. "
    "Rules:\n"
    "- Pick the company's own corporate/brand website, not third-party listings.\n"
    "- Reject directories, review sites, job boards, company registries, social media.\n"
    "- The domain does NOT have to contain the company name: sister brands, "
    "acronyms and service+city domains are fine IF the page text clearly names "
    "the company.\n"
    "- If the top candidates refer to a different company (name collision), reply NONE.\n"
    "- If the page places the company somewhere incompatible with the given "
    "location (a namesake elsewhere), reply NONE.\n"
    "- If no candidate is clearly the official site, reply NONE."
)


def llm_pick_domain(company, city, state, candidates):
    """Ask GPT-4.1 to pick the official website from pre-filtered candidates.
    Returns the chosen bare domain (guaranteed to be one of the candidates),
    or ''."""
    if not candidates or not AZ_ENDPOINT:
        return ""
    loc = ", ".join(x for x in [city, state] if x) or "unknown"
    lines = [f"Company: {company}", f"Location: {loc}", "", "Candidates:"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {c['domain']}")
        lines.append(f"   Title: {c['title'][:150]}")
        lines.append(f"   Page text: {c['text'][:300]}")
    lines.append("")
    lines.append("Reply with ONLY the bare domain or NONE.")
    try:
        resp = requests.post(
            f"{AZ_ENDPOINT}/openai/deployments/{AZ_MODEL}/chat/completions",
            params={"api-version": AZ_VERSION},
            headers={"api-key": AZ_KEY, "Content-Type": "application/json"},
            json={"messages": [{"role": "system", "content": LLM_SYSTEM},
                               {"role": "user", "content": "\n".join(lines)}],
                  "max_completion_tokens": 60,
                  "temperature": 0},
            timeout=60)
        if resp.status_code != 200:
            return ""
        answer = (resp.json()["choices"][0]["message"]["content"] or "").strip().lower()
        answer = re.sub(r"^https?://", "", answer).split("/")[0].strip().strip(".,'\"`")
        answer = re.sub(r"^www\.", "", answer)
        if not answer or answer == "none" or "." not in answer:
            return ""
        return answer if answer in {c["domain"] for c in candidates} else ""
    except Exception:
        return ""


def judge(company, city, state, results, taken):
    """Mechanically prefilter Exa results, then let GPT-4.1 pick — or NONE."""
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
                           "title": item.get("title") or "",
                           "text": item.get("text") or ""})

    if not candidates:
        return "", "no_match"
    dom = llm_pick_domain(company, city, state, candidates)
    return (dom, "ok_llm") if dom else ("", "no_match")


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
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    key = os.getenv("EXA_API_KEY")
    if not key:
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

    todo = []
    for i, r in enumerate(rows):
        name = cell(r, C)
        if not name:
            continue
        if cell(r, W) and "." in cell(r, W) and not args.overwrite:
            continue
        # A prior attempt that ended no_match/ok_verify leaves the website cell
        # BLANK but stamps the status column — without this check those rows
        # sit at the top of the sheet and get re-searched (and re-billed) on
        # every --limit run before any fresh row is reached. Re-attempt them
        # only with --retry_attempted.
        if (STA is not None and cell(r, STA).startswith("exa_")
                and not args.overwrite and not args.retry_attempted):
            continue
        todo.append({"row": i + 2, "company": name,
                     "city": cell(r, CY), "state": cell(r, ST)})
    if args.limit:
        todo = todo[:args.limit]

    print(f"=== Exa website enrichment {'(APPLY)' if args.apply else '(DRY RUN)'} ===")
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
    results_out = []

    def work(t):
        res, cost, err = exa_search(t["company"], t["city"], t["state"],
                                    args.niche, key)
        with lock:
            spent[0] += cost
        if err:
            with lock:
                stats[err] += 1
                results_out.append((t, "", err))
            return
        with lock:
            snapshot = dict(taken)
        dom, status = judge(t["company"], t["city"], t["state"], res, snapshot)
        with lock:
            if dom:
                # claim it immediately so two rows in the same batch cannot both
                # take the same parent domain
                if dom in taken and taken[dom].lower() != t["company"].lower():
                    dom, status = "", "domain_claimed_by_other_company"
                else:
                    taken[dom] = t["company"]
            stats[status] += 1
            results_out.append((t, dom, status))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pool.map(work, todo)

    writes = []
    for t, dom, status in results_out:
        if dom and status == "ok_llm":
            writes.append({"range": f"{args.tab}!{idx_col(W)}{t['row']}",
                           "values": [[dom]]})
        if STA is not None:
            writes.append({"range": f"{args.tab}!{idx_col(STA)}{t['row']}",
                           "values": [[f"exa_{status}"]]})
    for k in range(0, len(writes), 100):
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={"valueInputOption": "RAW", "data": writes[k:k + 100]}).execute()

    found = sum(1 for _, d, _ in results_out if d)
    print(f"\n=== Summary ===")
    print(f"  resolved:     {found}/{len(todo)} ({100*found/max(1,len(todo)):.0f}%)")
    print(f"  Exa spend:    ${spent[0]:.3f}")
    for k, n in stats.most_common():
        print(f"    {k:36} {n}")
    picked = [(t['company'], d) for t, d, s in results_out if s == "ok_llm"]
    if picked:
        print(f"\n  LLM picks (spot-check a few):")
        for nm, d in picked[:20]:
            print(f"    {nm[:40]:42} -> {d}")


if __name__ == "__main__":
    main()
