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

The rule that survives both: **a domain is only as good as the evidence on the
page.** So this script never accepts a domain because it looks like the name.

WHAT IS DIFFERENT FROM THE ROOFING SKILL THIS IS MODELLED ON
------------------------------------------------------------
That skill requires the company's identity prefix to appear IN the domain
("Oak Valley Roofing" -> oakvalleyroofing.com) and rejects anything else. That
rule is right for roofing and wrong for healthcare: practices routinely trade
under a sister brand or a service+city domain. Two verified-correct examples
from Jude's own sheet that the prefix rule would have thrown away:

    Easy Reach PT Rehab        -> easyreachchiro.com     (sister brand)
    Pamela Rowe Speech Therapy -> speechorlando.com      (service + city)

So the prefix-in-domain test is kept only as a FAST PATH, never as a gate.
Everything that fails it goes to content verification instead of being dropped.

ACCEPTANCE (a domain is written only if one of these holds)
-----------------------------------------------------------
  A. prefix match — the company's first two distinctive words appear in the
     domain. Cheap, unambiguous, no content needed.        -> status ok_prefix
  B. content match — the page text names the company (majority of its
     distinctive tokens) AND corroborates the location (city or state).
     This is what rescues sister-brand and service+city domains. -> ok_content
  C. weak match — company named on the page but no location corroboration, or
     the company name has only one distinctive token. Written, but flagged for
     a human glance.                                        -> ok_verify

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
)

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

# Words that carry no identity — they must never be what proves a match.
GENERIC = {
    "health", "healthcare", "medical", "medicine", "care", "center", "centre",
    "clinic", "clinics", "group", "associates", "partners", "services",
    "service", "institute", "practice", "specialists", "specialist",
    "solutions", "systems", "system", "physicians", "physician", "doctors",
    "therapy", "therapies", "rehab", "rehabilitation", "wellness", "family",
    "community", "regional", "national", "american", "advanced", "premier",
    "quality", "professional", "comprehensive", "complete", "total", "the",
    "and", "of", "at", "for", "inc", "llc", "pllc", "pc", "pa", "llp", "ltd",
    "corp", "corporation", "company", "co", "hospital", "home", "nursing",
}
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
    if h.endswith(".gov"):
        return "government"
    return None


def words(name):
    n = LEGAL.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return [w for w in n.split() if w]


def distinctive(name):
    return [w for w in words(name) if w not in GENERIC and len(w) > 2]


def identity_prefix(name):
    """First TWO distinctive words joined, or "" when the name has fewer.

    Two is not arbitrary. "Orlando Health" has one distinctive word ("health"
    is generic), and a one-word prefix matches anything containing it —
    orlando.org (the Orlando Economic Partnership) sails straight through.
    A single distinctive token is not an identity, so such names get no fast
    path at all and must be proven on page content."""
    d = distinctive(name)
    if len(d) < 2:
        return ""
    return "".join(d[:2])


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


def judge(company, city, state, results, taken):
    """Pick a domain, or return ('', reason). Never guesses."""
    toks = distinctive(company)
    prefix = identity_prefix(company)
    city_l, state_l = (city or "").lower(), (state or "").lower()
    weak = None

    for item in results:
        dom = norm_domain(item.get("url", ""))
        if not dom or "." not in dom:
            continue
        why_junk = is_junk(dom)
        if why_junk:
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

        bare = dom.replace("-", "")
        # A0. EXACT full-name match — the registrable name IS the company name.
        # Conclusive on its own, no location needed: "The Neurology Institute" ->
        # theneurologyinstitute.com cannot be a coincidence, whereas a single
        # shared token ("orlando" in orlando.org) trivially can. Compared both
        # with and without the legal suffix, since "No Limits Therapy, Inc." ->
        # nolimitstherapyinc.com keeps the "inc".
        root_name = bare.rsplit(".", 1)[0] if "." in bare else bare
        full_legal = "".join(re.sub(r"[^a-z0-9 ]", " ", (company or "").lower()).split())
        full_clean = "".join(words(company))
        if root_name and root_name in (full_legal, full_clean):
            return dom, "ok_exact_name"

        # A. fast path — identity prefix (2+ distinctive words) visible in the
        # domain. Empty prefix means the name was too thin to be an identity.
        if prefix and len(prefix) >= 6 and prefix in bare:
            return dom, "ok_prefix"

        # B. content path — the page must name the company AND place it
        blob = ((item.get("title") or "") + " " + (item.get("text") or "")).lower()
        if not blob.strip():
            continue
        hit = [t for t in toks if t in blob]
        named = toks and len(hit) >= max(1, (len(toks) + 1) // 2)
        located = bool((city_l and city_l in blob) or (state_l and state_l in blob))
        if named and located and len(toks) >= 2:
            return dom, "ok_content"
        if named and weak is None:
            weak = dom      # remember, but keep looking for something stronger

    if weak:
        # Named on the page but nothing corroborates WHICH company or where it
        # is. That is exactly how orlando.org and lhc.org (a church in Austin)
        # got picked by the previous approach, so this is NOT written by
        # default — it is surfaced for a human instead.
        return weak, "ok_verify"
    return "", "no_match"


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
    ap.add_argument("--accept_weak", action="store_true",
                    help="also write ok_verify matches (company named on the page "
                         "but no location corroboration). OFF by default — a blank "
                         "cell beats a wrong domain.")
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

    STRONG = {"ok_exact_name", "ok_prefix", "ok_content"}
    writes = []
    for t, dom, status in results_out:
        if dom and (status in STRONG or args.accept_weak):
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
    verify = [(t['company'], d) for t, d, s in results_out if s == "ok_verify"]
    if verify:
        state = "WRITTEN (--accept_weak)" if args.accept_weak else "NOT written"
        print(f"\n  {len(verify)} weak matches, {state} — company named on the page "
              f"but location unconfirmed:")
        for nm, d in verify[:20]:
            print(f"    {nm[:40]:42} -> {d}")


if __name__ == "__main__":
    main()
