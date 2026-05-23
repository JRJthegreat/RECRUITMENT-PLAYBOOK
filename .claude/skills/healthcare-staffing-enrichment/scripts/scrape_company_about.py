"""
Phase 1.5: Scrape the about page of each company's website and summarize it.

For each company with a website, tries /about, /about-us, /company, / (homepage)
in order until a page with sufficient content is found. Strips HTML boilerplate
and sends the text to Azure OpenAI for a 2-3 sentence summary. Writes to the
company_about column (P, index 15).

Run:
  python3 -W ignore scrape_company_about.py --sheet_url "URL" [--limit N]
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
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

SHEET_ID = "1b0PSJncVDZJ_-iz5IMB6GdPcWgZQ3F85XMpiL8A1rL4"
TAB_NAME = "dataset_healthcare-recruitment-agencies_2026-05-17_13-09-00-863"
COL_NAME = 3      # D: company_name
COL_WEBSITE = 12  # M: website
COL_ABOUT = 17    # R: company_about (new; O+P taken by classification)

WRITE_BATCH = 10
HTTP_WORKERS = 5
HTTP_TIMEOUT = 12
MIN_TEXT_LEN = 200
MAX_TEXT_LEN = 1500

ABOUT_PATHS = ["/about", "/about-us", "/about-us/", "/company", "/our-story", "/who-we-are", "/"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

SUMMARIZE_SYSTEM = (
    "Summarize what this healthcare staffing company does in 2-3 sentences. "
    "Be specific about what types of healthcare workers they place (nurses, physicians, therapists, etc.) "
    "and who their clients are (hospitals, clinics, etc.). "
    "Return only the summary, no preamble."
)


def col_letter(idx):
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


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


def ensure_header(service):
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB_NAME}'!{col_letter(COL_ABOUT)}1",
        valueInputOption="RAW",
        body={"values": [["company_about"]]},
    ).execute()
    print("  Set header: company_about")


def fetch_page_text(base_url):
    base = base_url.rstrip("/")
    for path in ABOUT_PATHS:
        url = base + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT,
                                allow_redirects=True)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("content-type", "")
            if "text/html" not in ct and "html" not in ct:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header",
                              "aside", "noscript", "iframe"]):
                tag.decompose()
            text = soup.get_text(" ", strip=True)
            text = re.sub(r"\s{2,}", " ", text).strip()
            if len(text) >= MIN_TEXT_LEN:
                return text[:MAX_TEXT_LEN]
        except Exception:
            continue
    return None


def summarize(client, text):
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=200,
            temperature=0,
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM},
                {"role": "user", "content": text},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return ""


def process_one(item, client):
    text = fetch_page_text(item["website"])
    if not text:
        return item, None
    summary = summarize(client, text)
    return item, summary


def flush_updates(service, updates):
    if not updates:
        return
    data = [
        {"range": f"'{TAB_NAME}'!{col_letter(COL_ABOUT)}{u['row']}", "values": [[u["summary"]]]}
        for u in updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    print(f"  -> Wrote {len(updates)} summaries", flush=True)
    time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", default=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    service = get_service()
    ensure_header(service)

    print("=== Phase 1.5: Scrape Company About Pages ===\n", flush=True)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB_NAME}'!A:Z"
    ).execute().get("values", [])[1:]

    targets = []
    for i, row in enumerate(rows):
        name = row[COL_NAME] if len(row) > COL_NAME else ""
        website = row[COL_WEBSITE] if len(row) > COL_WEBSITE else ""
        about = row[COL_ABOUT] if len(row) > COL_ABOUT else ""
        if name.strip() and website.strip() and not about.strip():
            targets.append({"row": i + 2, "name": name.strip(), "website": website.strip()})

    if args.limit:
        targets = targets[:args.limit]
    print(f"  {len(targets)} companies need about summary\n", flush=True)

    if not targets:
        print("Nothing to do."); return

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION,
    )

    updates = []
    found = skipped = 0
    total = len(targets)

    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as ex:
        future_map = {ex.submit(process_one, t, client): t for t in targets}
        for i, fut in enumerate(as_completed(future_map), 1):
            item, summary = fut.result()
            if summary:
                found += 1
                updates.append({"row": item["row"], "summary": summary})
                print(f"  +  {item['name'][:50]:50s} → {summary[:60]}...", flush=True)
            else:
                skipped += 1
                print(f"  x  {item['name'][:50]:50s} → (no content)", flush=True)

            if len(updates) >= WRITE_BATCH:
                flush_updates(service, updates)
                updates = []

            if i % 50 == 0:
                print(f"  Progress: {i}/{total}", flush=True)

    if updates:
        flush_updates(service, updates)

    print(f"\nSummary: Got {found} summaries, skipped {skipped} / {total}", flush=True)


if __name__ == "__main__":
    main()
