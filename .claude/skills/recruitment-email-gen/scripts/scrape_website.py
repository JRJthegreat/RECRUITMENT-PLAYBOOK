"""
Scrape each company's website for richer ICP/role signal (niche-agnostic).

For every row with a website, fetches the recruiter-relevant pages (about,
services, sectors, specialisms, homepage), strips boilerplate, and summarizes
with Azure OpenAI GPT-4.1 into a tight 2-4 sentence blurb focused on:
  - the industries / types of employers they recruit for
  - the specific roles / specialisms they fill

Writes the summary to --col_out. generate_email_body.py then reads that column
(via --col_desc) for far better ICP/role extraction than the Apollo blurb.

Direct HTTP (no Apify cost). Batch-of-10 writes. Resume-safe: skips rows where
--col_out is already populated.

Run:
  python3 -W ignore scrape_website.py --sheet_url "URL" --tab "TAB" \
    --col_website J --col_out AA [--col_status R --status_value found] [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
import requests
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
TARGET_LEN   = 1800   # stop gathering pages once we have this much text
MAX_TEXT_LEN = 2500   # hard cap sent to the LLM

# Recruiter-relevant pages first (sectors/services/specialisms carry ICP + roles).
CANDIDATE_PATHS = [
    "/about", "/about-us", "/who-we-are",
    "/services", "/sectors", "/industries", "/specialisms", "/what-we-do",
    "/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

SUMMARIZE_SYSTEM = (
    "You summarize a recruitment agency from its website text, in 2-4 sentences. "
    "Be specific about (1) the industries and types of employers/clients they recruit for, "
    "and (2) the specific job roles or specialisms they fill. "
    "Only use information supported by the text. Do not invent facts. "
    "Return only the summary, no preamble."
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


def fetch_one_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        ct = resp.headers.get("content-type", "")
        if "html" not in ct:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
            tag.decompose()
        text = re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True)).strip()
        return text if len(text) >= MIN_PAGE_LEN else ""
    except Exception:
        return ""


def gather_site_text(website):
    """Pull a few recruiter-relevant pages and concatenate until TARGET_LEN."""
    base = normalize_url(website)
    if not base:
        return ""
    chunks, total = [], 0
    for path in CANDIDATE_PATHS:
        text = fetch_one_page(base + path)
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total >= TARGET_LEN:
            break
    return (" \n\n ".join(chunks))[:MAX_TEXT_LEN] if chunks else ""


def summarize(client, text):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=220,
            temperature=0,
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM},
                {"role": "user", "content": text},
            ],
        )
        return (resp.choices[0].message.content or "").strip().replace("—", ",").replace("–", ",")
    except Exception:
        return ""


def process_one(item, client):
    text = gather_site_text(item["website"])
    if not text:
        return item["row"], None
    return item["row"], summarize(client, text)


def ensure_col(service, sheet_id, tab_name, c_out):
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
        body={"values": [["web_summary"]]},
    ).execute()


def flush(service, updates, sheet_id, tab_name, c_out):
    if not updates:
        return
    data = [
        {"range": f"'{tab_name}'!{col_letter(c_out)}{r}", "values": [[s]]}
        for r, s in updates
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
    parser.add_argument("--col_website", default="J")
    parser.add_argument("--col_out", default="AA")
    parser.add_argument("--col_status", default="", help="Optional status column filter (blank = no filter)")
    parser.add_argument("--status_value", default="found")
    parser.add_argument("--limit", type=int, default=0, help="Cap number of rows processed")
    parser.add_argument("--preview", type=int, default=0, help="Scrape + print N without writing")
    args = parser.parse_args()

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
        ensure_col(service, sheet_id, tab_name, c_out)

    last_col = col_letter(max(c_web, c_out, c_stat or 0))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        if c_stat is not None:
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

    print("=== Scrape Websites ===\n")
    print(f"Rows to scrape: {len(pending)}\n")

    if args.preview:
        pending = pending[:args.preview]
        with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
            futs = {ex.submit(process_one, p, client): p for p in pending}
            for fut in as_completed(futs):
                p = futs[fut]
                row, summary = fut.result()
                print(f"--- Row {row}  |  {p['website']} ---")
                print(summary if summary else "(no content scraped)")
                print()
        return

    updates, done, ok = [], 0, 0
    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
        futs = {ex.submit(process_one, p, client): p for p in pending}
        for fut in as_completed(futs):
            row, summary = fut.result()
            done += 1
            if summary:
                updates.append((row, summary))
                ok += 1
            if len(updates) >= WRITE_BATCH:
                flush(service, updates, sheet_id, tab_name, c_out)
                updates = []
            if done % 25 == 0:
                print(f"  ...{done}/{len(pending)} scraped ({ok} with content)", flush=True)
    if updates:
        flush(service, updates, sheet_id, tab_name, c_out)

    print(f"\nDone - {ok}/{len(pending)} rows got a web summary.")


if __name__ == "__main__":
    main()
