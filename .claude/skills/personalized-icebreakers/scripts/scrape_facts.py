"""
Phase 1 - scrape each company's website and extract CONCRETE, CHECKABLE facts.

Niche-agnostic. Fetches the pages that actually carry founder/leadership history
and hard numbers (/team, /our-story, /leadership, /founder first), strips
boilerplate, and asks GPT-4.1 to extract four fact slots as strict JSON:

    founder_background   prior career history BEFORE the firm
    published_numbers    years in business, placements, time-to-fill, fill rate
    niche                the specific vertical, narrower than "staffing"
    credentials          named certifications / memberships

Any slot the page text does not support comes back as "". A site that is pure
marketing boilerplate returns all four empty, which is the correct outcome:
generate_icebreaker.py then falls back to the static line rather than inventing
a fake-personalized opener.

Writes the JSON blob to --col_out. Direct HTTP (no Apify cost).
Batch-of-10 writes. Resume-safe: skips rows where --col_out is already filled.

Run:
  python3 -W ignore scrape_facts.py --sheet_url "URL" --tab "TAB" \
    --col_website L --col_out AN [--profile founder|icp] \
    [--col_status AB --status_value valid] [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4 lxml")
    raise

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

WRITE_BATCH  = 10
HTTP_WORKERS = 6
HTTP_TIMEOUT = 10
MIN_PAGE_LEN = 150

# Whole-site crawl, not a fixed path list. Fixed paths miss most of a small
# agency site: the founder story is as likely to sit at /our-difference or
# /why-us as at /about. We seed with the high-value paths, then discover the
# rest by following internal links from the homepage.
MAX_PAGES      = 25      # hard cap on pages fetched per site
TARGET_LEN     = 14000   # stop crawling once we have this much text
MAX_TEXT_LEN   = 18000   # hard cap sent to the LLM
SITE_TIME_CAP  = 75      # seconds per site, so one slow host cannot stall a run

# Per-page abstracts (Saraev's method). Each page is summarised on its own so a
# buried founder bio carries the same weight as the homepage banner. Costs one
# LLM call per page, so the budget is capped at the top-scoring pages rather
# than all 25: that keeps the non-obvious-detail benefit at ~a third the spend.
ABSTRACT_PAGES = 10      # pages per site that get their own abstract
PAGE_CHAR_CAP  = 5000    # chars of a single page sent to the model

# Never worth fetching: assets, and pages that are always boilerplate.
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4",
            ".mp3", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ico", ".css", ".js")
SKIP_WORDS = ("privacy", "terms", "cookie", "disclaimer", "login", "signin",
              "sign-in", "register", "cart", "checkout", "sitemap", "feed",
              "wp-content", "wp-admin", "/tag/", "/category/", "/author/",
              "unsubscribe", "accessibility")

# Founder background is the highest-value fact and the scarcest, so the pages
# that carry it are fetched FIRST. gather_site_text() stops at TARGET_LEN, so
# ordering decides what the model actually sees.
PATHS_FOUNDER = [
    "/team", "/our-team", "/meet-the-team", "/leadership",
    "/our-story", "/founder", "/about/team",
    "/about", "/about-us", "/who-we-are",
    "/services", "/sectors", "/specialisms",
    "/",
]

# Original recruitment-email-gen ordering, for ICP/role signal instead.
PATHS_ICP = [
    "/about", "/about-us", "/who-we-are",
    "/services", "/sectors", "/industries", "/specialisms", "/what-we-do",
    "/",
]

# A bare UA + Accept pair gets 403'd by common WAFs: on a 14-domain sample,
# 5 of 6 "unreachable" sites were bot-protection pages, not dead hosts (three
# returned an identical 75,193-byte challenge). Sending the full set of headers
# a real Chrome sends recovered 4 of those 6. Keep this complete.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="126", "Not)A;Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

FACT_KEYS = ["founder_background", "published_numbers", "awards",
             "milestone", "niche", "credentials"]

EXTRACT_SYSTEM = (
    "You extract concrete, checkable facts about a recruitment agency from its "
    "own website text.\n\n"
    "Return STRICT JSON with exactly these keys: founder_background, "
    "published_numbers, awards, milestone, niche, credentials. Each value is a "
    "short factual phrase drawn from the text, or \"\" if the text does not "
    "support it.\n\n"
    "founder_background: the founder's or a leader's professional history "
    "BEFORE this firm. Examples: \"founder spent 12 years as an OR nurse\", "
    "\"started by a former hospital CFO\". Only if prior career history is "
    "actually stated. A founding year is NOT a background.\n"
    "  IMPORTANT: if the text names the person, KEEP THE NAME in your answer "
    "(\"Aimee Brewer spent 24 years as an RN\"). The downstream step needs the "
    "name to work out whether it is writing to that person.\n"
    "  Capture only the SUBSTANTIVE part of the career. Skip student jobs, "
    "menial early roles, and personal hardship. \"worked as a janitor during "
    "college\" must NOT be returned even if the site says it.\n"
    "published_numbers: a specific figure the site publishes. Years in "
    "business, placements made, average time to fill, retention or fill rate, "
    "number of client sites.\n"
    "awards: a NAMED award, ranking or recognition, ideally with a year. "
    "\"Inc 5000 in 2023\", \"Best of Staffing 2024\". Generic \"award-winning\" "
    "is not an award, return \"\".\n"
    "milestone: a concrete company event. New office or market, an "
    "anniversary, an acquisition, a named expansion. Must be a specific event, "
    "not \"we are growing\".\n"
    "niche: the specific vertical they serve. Only return this if it is "
    "genuinely NARROW and unusual. \"behavioral health only\", \"FQHCs and "
    "community clinics\", \"perioperative nursing\" qualify. Broad lists like "
    "\"healthcare, IT and finance\" or generic \"healthcare staffing\" do NOT, "
    "return \"\" for those.\n"
    "credentials: named certifications or memberships. Joint Commission, SIA "
    "listing, NAHCR, state licensure.\n\n"
    "Rules: use only what the text supports. Never infer, never invent. Most "
    "agency websites are generic marketing copy with no concrete facts. When "
    "that is the case, returning all four empty is the CORRECT answer and is "
    "strongly preferred over stretching to fill a slot.\n"
    "Return only the JSON object."
)


def col_to_idx(letter):
    letter = letter.strip().upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_service():
    with open(TOKEN_PATH) as f:
        td = json.load(f)
    creds = Credentials(
        token=td["token"], refresh_token=td["refresh_token"],
        token_uri=td["token_uri"], client_id=td["client_id"],
        client_secret=td["client_secret"],
        scopes=td.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        td["token"] = creds.token
        with open(TOKEN_PATH, "w") as f:
            json.dump(td, f)
    return build("sheets", "v4", credentials=creds)


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def normalize_url(website):
    w = website.strip()
    if not w:
        return ""
    if not w.startswith(("http://", "https://")):
        w = "https://" + w
    return w.rstrip("/")


def fetch_one_page(url, session=None):
    """Return (text, discovered_links). Links are only harvested here so the
    crawler does not need a second request per page."""
    try:
        getter = session or requests
        resp = getter.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return "", []
        if "html" not in resp.headers.get("content-type", ""):
            return "", []
        soup = BeautifulSoup(resp.text, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            links.append(a["href"])
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
            tag.decompose()
        text = re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True)).strip()
        return (text if len(text) >= MIN_PAGE_LEN else ""), links
    except Exception:
        return "", []


def want(url):
    low = url.lower()
    if low.endswith(SKIP_EXT):
        return False
    return not any(w in low for w in SKIP_WORDS)


def score(path):
    """Crawl the pages most likely to carry founder history and numbers first."""
    p = path.lower()
    for i, group in enumerate([
        ("/team", "/our-team", "/meet", "/leadership", "/our-story", "/founder", "/bio"),
        # Recency beats evergreen: a dated press item from the last few months
        # is the strongest icebreaker material there is, so it outranks /about.
        ("/news", "/press", "/award", "/announcement", "/case-stud", "/testimonial", "/partnership"),
        ("/about", "/who-we-are", "/why", "/our-difference", "/history", "/mission"),
        ("/service", "/sector", "/industr", "/specialis", "/expertise", "/what-we-do", "/practice"),
    ]):
        if any(g in p for g in group):
            return i
    return 4


def gather_pages(website, paths):
    """Crawl the site and return [(url, text), ...] with pages kept SEPARATE.

    Keeping them separate is the point. Concatenating every page into one blob
    means the model latches onto whatever is most prominent and repeated, which
    is the homepage headline: precisely the obvious detail we do not want. It
    also silently truncates the tail, so a founder bio buried on page 19 never
    reaches the model. Summarising each page independently gives a small page
    the same weight as the homepage, which is where the non-obvious specifics
    that read as genuine research actually live.

    Bounded three ways so one pathological host cannot stall a 2,000-row run:
    MAX_PAGES, TARGET_LEN of collected text, and SITE_TIME_CAP wall clock.
    """
    base = normalize_url(website)
    if not base:
        return []
    host = urlparse(base).netloc.lower().replace("www.", "")
    started = time.time()

    # One session per site so cookies set by the WAF on the first hit carry to
    # the subsequent page fetches. Hitting the homepage first also avoids
    # opening with a run of 404s, which reads as scraping and gets us blocked.
    session = requests.Session()
    session.headers.update(HEADERS)

    home_text, home_links = fetch_one_page(base + "/", session)

    # If the homepage yields neither text nor links the host is blocking us or
    # is dead. Crawling 25 more pages will not change that, and on a 2,000-row
    # run those wasted fetches dominate wall clock (one blocked site burned 80s
    # to return nothing). Bail immediately.
    if not home_text and not home_links:
        return []

    seen = {base + "/", base}
    queue = []

    # Seed with the known-valuable paths, then everything the homepage links to.
    for p in paths:
        if p != "/":
            u = base + p
            if u not in seen:
                seen.add(u)
                queue.append(u)
    for href in home_links:
        u = urljoin(base + "/", href.strip())
        u, _, _ = u.partition("#")
        if not u or u in seen:
            continue
        if urlparse(u).netloc.lower().replace("www.", "") != host:
            continue          # same site only
        if not want(u):
            continue
        seen.add(u)
        queue.append(u)

    queue.sort(key=lambda u: score(urlparse(u).path))

    pages = [(base + "/", home_text)] if home_text else []
    total = len(home_text or "")
    fetched = 1

    while queue and fetched < MAX_PAGES and total < TARGET_LEN:
        if time.time() - started > SITE_TIME_CAP:
            break
        url = queue.pop(0)
        text, links = fetch_one_page(url, session)
        fetched += 1
        if not text:
            continue
        pages.append((url, text))
        total += len(text)
        # Follow one level deeper, but only into high-value pages, so we do not
        # wander into paginated blog archives.
        for href in links[:60]:
            u = urljoin(url, href.strip())
            u, _, _ = u.partition("#")
            if not u or u in seen:
                continue
            if urlparse(u).netloc.lower().replace("www.", "") != host:
                continue
            if not want(u) or score(urlparse(u).path) >= 3:
                continue
            seen.add(u)
            queue.append(u)

    # Best pages first, so the per-page abstract budget is spent where the
    # founder history and hard numbers actually live.
    pages.sort(key=lambda p: score(urlparse(p[0]).path))
    return pages


def clean(s):
    return (s or "").strip().replace("—", ", ").replace("–", ", ")


ABSTRACT_SYSTEM = (
    "You summarize ONE page of a recruitment agency's website.\n\n"
    "Write a dense two-paragraph abstract of what this page says, at a similar "
    "level of detail as the abstract of a published paper. Straightforward, "
    "Spartan tone of voice. No marketing language, no adjectives of your own.\n\n"
    "Capture SPECIFICS above all: names of people and their prior employers and "
    "job history, numbers of any kind (years, placements, rates, headcount, "
    "founding dates), named clients, named certifications and associations, "
    "named awards, named locations, named job titles they recruit for, and any "
    "unusual or oddly specific detail.\n\n"
    "The small, non-obvious details are the most valuable part of your output. "
    "A buried sentence about the founder's previous career, or a named local "
    "association membership, matters MORE than the headline banner claim. Do "
    "not skip a detail because it seems minor.\n\n"
    "If the page is boilerplate with no concrete content (a contact form, a "
    "privacy notice, a bare list of links), reply with exactly: SKIP"
)


def abstract_page(client, url, text):
    """Summarize a single page. Per-page abstracts are what surface the buried
    specifics: in one concatenated blob the homepage headline drowns them out."""
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=400,
            temperature=0.1,
            messages=[
                {"role": "system", "content": ABSTRACT_SYSTEM},
                {"role": "user", "content": f"URL: {url}\n\n{text[:PAGE_CHAR_CAP]}"},
            ],
        )
        out = clean(resp.choices[0].message.content)
    except Exception:
        return None
    if not out or out.strip().upper().startswith("SKIP"):
        return None
    return out[:1600]


def process_one(item, client, paths):
    pages = gather_pages(item["website"], paths)
    if not pages:
        return item["row"], None
    pages = pages[:ABSTRACT_PAGES]
    abstracts = []
    # Small inner pool: these are IO-bound API calls and the outer pool is
    # already running HTTP_WORKERS sites at once.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(abstract_page, client, u, t): u for u, t in pages}
        for fut in as_completed(futs):
            u = futs[fut]
            a = fut.result()
            if a:
                abstracts.append({"url": u, "abstract": a})
    if not abstracts:
        return item["row"], {"pages": [], "n": 0}
    return item["row"], {"pages": abstracts, "n": len(abstracts)}


def ensure_col(service, sheet_id, tab_name, c_out, header):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    if current_cols < c_out + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": c_out + 1 - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(c_out)}1",
        valueInputOption="RAW",
        body={"values": [[header]]},
    ).execute()


def flush(service, updates, sheet_id, tab_name, c_out):
    if not updates:
        return
    data = [
        {"range": f"'{tab_name}'!{col_letter(c_out)}{r}", "values": [[v]]}
        for r, v in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_website", default="L")
    parser.add_argument("--col_out", default="AN")
    parser.add_argument("--profile", choices=["founder", "icp"], default="founder",
                        help="founder = fetch /team,/our-story,/leadership first (default)")
    parser.add_argument("--col_status", default="")
    parser.add_argument("--status_value", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--preview", type=int, default=0, help="Scrape + print N, write nothing")
    args = parser.parse_args()

    paths = PATHS_FOUNDER if args.profile == "founder" else PATHS_ICP

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab
    c_web  = col_to_idx(args.col_website)
    c_out  = col_to_idx(args.col_out)
    c_stat = col_to_idx(args.col_status) if args.col_status else None
    status_value = args.status_value.strip().lower()

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION,
    )
    service = get_service()
    if not args.preview:
        ensure_col(service, sheet_id, tab_name, c_out, "page_abstracts")

    last_col = col_letter(max(c_web, c_out, c_stat or 0))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        if c_stat is not None and status_value:
            status = row[c_stat].strip().lower() if len(row) > c_stat else ""
            if status != status_value:
                continue
        website  = row[c_web].strip() if len(row) > c_web else ""
        existing = row[c_out].strip() if len(row) > c_out else ""
        if not website or existing:
            continue
        pending.append({"row": i + 2, "website": website})

    if args.limit:
        pending = pending[:args.limit]

    print(f"=== Scrape Facts (profile: {args.profile}) ===\n")
    print(f"Rows to scrape: {len(pending)}\n")

    if args.preview:
        pending = pending[:args.preview]
        npages, none_at_all = [], 0
        with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
            futs = {ex.submit(process_one, p, client, paths): p for p in pending}
            for fut in as_completed(futs):
                p = futs[fut]
                row, res = fut.result()
                print(f"--- Row {row}  |  {p['website']} ---")
                if not res or not res.get("pages"):
                    print("  (site unreachable / blocked / no usable text)")
                    none_at_all += 1
                else:
                    npages.append(res["n"])
                    print(f"  {res['n']} page abstracts:")
                    for pg in res["pages"]:
                        print(f"    [{pg['url']}]")
                        print(f"      {pg['abstract'][:300]}")
                print()
        print("=== COVERAGE ===")
        print(f"  sites with abstracts : {len(npages)}/{len(pending)}")
        print(f"  unusable             : {none_at_all}/{len(pending)}")
        if npages:
            print(f"  avg pages per site   : {sum(npages)/len(npages):.1f}")
        return

    updates, done, ok = [], 0, 0
    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
        futs = {ex.submit(process_one, p, client, paths): p for p in pending}
        for fut in as_completed(futs):
            row, res = fut.result()
            done += 1
            if res is not None:
                updates.append((row, json.dumps(res, ensure_ascii=False)[:48000]))
                if res.get("pages"):
                    ok += 1
            if len(updates) >= WRITE_BATCH:
                flush(service, updates, sheet_id, tab_name, c_out)
                updates = []
            if done % 25 == 0:
                print(f"  ...{done}/{len(pending)} crawled ({ok} with page abstracts)", flush=True)
    if updates:
        flush(service, updates, sheet_id, tab_name, c_out)

    print(f"\nDone - {ok}/{len(pending)} rows have page abstracts.")


if __name__ == "__main__":
    main()
