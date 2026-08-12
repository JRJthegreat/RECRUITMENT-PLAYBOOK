"""
Phase 1 (icebreaker research) - crawl each company's website and collect the
raw text of its highest-value pages. NO LLM anywhere in this script (Jude's
call, 2026-08-12: the whole icebreaker pipeline stays LLM-free, not just the
final judgment).

Adapted from personalized-icebreakers/scripts/scrape_facts.py: keeps its
crawling engine verbatim (WAF-header set, homepage-first session, path
scoring that prioritizes /team /about /awards /press before generic pages,
MAX_PAGES/TARGET_LEN/SITE_TIME_CAP bounds) but drops the GPT-4.1 per-page
abstraction step entirely. Instead of an LLM-written summary, each kept page
carries its own cleaned raw text (capped per page) — Claude reads this
directly in the next phase rather than a pre-digested abstract.

Writes the JSON blob {"pages": [{"url", "text"}], "n": N} to --col_out.
Direct HTTP only (no Apify cost, no LLM cost). Batch-of-10 writes.
Resume-safe: skips rows where --col_out is already filled.

Run:
  python3 -W ignore scrape_company_facts.py --sheet_url "URL" [--tab Leads] \
    [--limit N] [--preview N]
"""
import os
import re
import time
import json
import argparse
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4 lxml")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

WRITE_BATCH = 10
HTTP_WORKERS = 6
HTTP_TIMEOUT = 10
MIN_PAGE_LEN = 150

MAX_PAGES = 25
TARGET_LEN = 14000
SITE_TIME_CAP = 75

# Pages kept for Claude to read, and how much raw text survives per page.
# Lower than the original's ABSTRACT_PAGES/PAGE_CHAR_CAP since there's no LLM
# compression step — this is what a subagent reads directly, so it has to
# stay a reasonable size across up to 261 leads' worth of research files.
KEEP_PAGES = 6
PAGE_CHAR_CAP = 2200

SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4",
            ".mp3", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ico", ".css", ".js")
SKIP_WORDS = ("privacy", "terms", "cookie", "disclaimer", "login", "signin",
              "sign-in", "register", "cart", "checkout", "sitemap", "feed",
              "wp-content", "wp-admin", "/tag/", "/category/", "/author/",
              "unsubscribe", "accessibility")

# Production-house equivalent of the original's PATHS_FOUNDER: pages most
# likely to carry a real founder story, notable client work, or awards.
PATHS_PRODUCTION = [
    "/team", "/our-team", "/about", "/about-us", "/who-we-are",
    "/work", "/clients", "/reel", "/awards", "/press", "/news",
    "/services", "/",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8"),
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


def col_letter(idx):
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def get_service():
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


def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot parse sheet ID from: {url}")
    return m.group(1)


def normalize_url(website):
    w = (website or "").strip()
    if not w:
        return ""
    if not w.startswith(("http://", "https://")):
        w = "https://" + w
    return w.rstrip("/")


def fetch_one_page(url, session=None):
    try:
        getter = session or requests
        resp = getter.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return "", []
        if "html" not in resp.headers.get("content-type", ""):
            return "", []
        soup = BeautifulSoup(resp.text, "lxml")
        links = [a["href"] for a in soup.find_all("a", href=True)]
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
    p = path.lower()
    for i, group in enumerate([
        ("/team", "/our-team", "/meet", "/founder", "/bio", "/who-we-are"),
        ("/news", "/press", "/award", "/announcement", "/case-stud", "/testimonial"),
        ("/about", "/why", "/our-story", "/history", "/work", "/clients", "/reel"),
        ("/service", "/what-we-do", "/capabilities"),
    ]):
        if any(g in p for g in group):
            return i
    return 4


def dedupe_key(u):
    return u.rstrip("/").lower()


def gather_pages(website):
    base = normalize_url(website)
    if not base:
        return []
    host = urlparse(base).netloc.lower().replace("www.", "")
    started = time.time()

    session = requests.Session()
    session.headers.update(HEADERS)

    home_text, home_links = fetch_one_page(base + "/", session)
    if not home_text and not home_links:
        return []

    seen = {dedupe_key(base + "/")}
    queue = []
    for p in PATHS_PRODUCTION:
        if p != "/":
            u = base + p
            if dedupe_key(u) not in seen:
                seen.add(dedupe_key(u))
                queue.append(u)
    for href in home_links:
        u = urljoin(base + "/", href.strip())
        u, _, _ = u.partition("#")
        if not u or dedupe_key(u) in seen:
            continue
        if urlparse(u).netloc.lower().replace("www.", "") != host:
            continue
        if not want(u):
            continue
        seen.add(dedupe_key(u))
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
        for href in links[:60]:
            u = urljoin(url, href.strip())
            u, _, _ = u.partition("#")
            if not u or dedupe_key(u) in seen:
                continue
            if urlparse(u).netloc.lower().replace("www.", "") != host:
                continue
            if not want(u) or score(urlparse(u).path) >= 3:
                continue
            seen.add(dedupe_key(u))
            queue.append(u)

    pages.sort(key=lambda p: score(urlparse(p[0]).path))
    return pages


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def process_one(item):
    pages = gather_pages(item["website"])
    if not pages:
        return item["row"], None
    kept = pages[:KEEP_PAGES]
    out = [{"url": u, "text": clean(t)[:PAGE_CHAR_CAP]} for u, t in kept]
    return item["row"], {"pages": out, "n": len(out)}


def ensure_col(service, sheet_id, tab_name, c_out, header):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    if current_cols < c_out + 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": c_out + 1 - current_cols}}]}).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!{col_letter(c_out)}1",
        valueInputOption="RAW", body={"values": [[header]]}).execute()


def flush(service, updates, sheet_id, tab_name, c_out):
    if not updates:
        return
    data = [{"range": f"'{tab_name}'!{col_letter(c_out)}{r}", "values": [[v]]} for r, v in updates]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


COL_WEBSITE = 11  # L
COL_DM_LINKEDIN = 29  # AD — scrape_dm_linkedin.py's output; gates who gets crawled
COL_OUT = 30      # AE — right after scrape_dm_linkedin.py's AD


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--preview", type=int, default=0)
    args = ap.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab
    service = get_service()
    if not args.preview:
        ensure_col(service, sheet_id, tab_name, COL_OUT, "site_pages_raw")

    last_col = col_letter(max(COL_WEBSITE, COL_DM_LINKEDIN, COL_OUT))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}").execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        website = row[COL_WEBSITE].strip() if len(row) > COL_WEBSITE else ""
        has_dm_linkedin = bool(row[COL_DM_LINKEDIN].strip()) if len(row) > COL_DM_LINKEDIN else False
        existing = row[COL_OUT].strip() if len(row) > COL_OUT else ""
        if not website or not has_dm_linkedin or existing:
            continue
        pending.append({"row": i + 2, "website": website})

    if args.limit:
        pending = pending[:args.limit]

    print(f"=== Scrape Company Facts (raw text, no LLM) ===\nRows to scrape: {len(pending)}\n")

    if args.preview:
        pending = pending[:args.preview]
        npages, none_at_all = [], 0
        with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
            futs = {ex.submit(process_one, p): p for p in pending}
            for fut in as_completed(futs):
                p = futs[fut]
                row, res = fut.result()
                print(f"--- Row {row}  |  {p['website']} ---")
                if not res or not res.get("pages"):
                    print("  (site unreachable / blocked / no usable text)")
                    none_at_all += 1
                else:
                    npages.append(res["n"])
                    print(f"  {res['n']} pages kept:")
                    for pg in res["pages"]:
                        print(f"    [{pg['url']}] {pg['text'][:200]}")
                print()
        print("=== COVERAGE ===")
        print(f"  sites with pages : {len(npages)}/{len(pending)}")
        print(f"  unusable         : {none_at_all}/{len(pending)}")
        return

    updates, done, ok = [], 0, 0
    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
        futs = {ex.submit(process_one, p): p for p in pending}
        for fut in as_completed(futs):
            row, res = fut.result()
            done += 1
            if res is not None:
                updates.append((row, json.dumps(res, ensure_ascii=False)[:48000]))
                if res.get("pages"):
                    ok += 1
            if len(updates) >= WRITE_BATCH:
                flush(service, updates, sheet_id, tab_name, COL_OUT)
                updates = []
            if done % 25 == 0:
                print(f"  ...{done}/{len(pending)} crawled ({ok} with pages)", flush=True)
    if updates:
        flush(service, updates, sheet_id, tab_name, COL_OUT)

    print(f"\nDone - {ok}/{len(pending)} rows have page text.")


if __name__ == "__main__":
    main()
