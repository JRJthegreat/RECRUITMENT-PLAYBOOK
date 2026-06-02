"""
Generate a personalized 1-sentence icebreaker for each row with a DM + email.

Sources used (all already in the sheet):
  DM name + title (AA/AB), company name (M), company description (W),
  job description (B), job title (A), openings detail (X), location (H).

Output: col AE "Icebreaker" on both Multiple Openings and Single Opening tabs.
Resume-safe: skips rows where AE already populated.
Only runs on rows that have both a DM name (AA) and email (AD).

Usage:
  python3 -W ignore generate_icebreakers.py --sheet_url "URL" [--preview 5] [--workers 8]

  --preview N   dry-run: show N generated icebreakers without writing
"""

import os
import json
import time
import argparse
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT   = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY    = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

TABS = ["Multiple Openings", "Single Opening"]
BATCH_SIZE = 10
LLM_WORKERS = 8

# Column indices (0-based)
COL_JOB_TITLE    = 0   # A
COL_JOB_DESC     = 1   # B
COL_LOCATION     = 7   # H
COL_COMPANY      = 12  # M
COL_COMPANY_DESC = 22  # W
COL_OPENINGS_DET = 24  # Y  (Openings Detail — multi tab only)
COL_DM_NAME      = 26  # AA
COL_DM_TITLE     = 27  # AB
COL_EMAIL        = 29  # AD
COL_ICEBREAKER   = 30  # AE
COL_CLEAN_CO     = 31  # AF

SYSTEM_PROMPT = """You write the opening line of a cold email. One sentence. Casual, human, specific.

The pattern to follow:
"Love how [Company] [specific thing they do or have built], [quick observation]."

The gold standard example:
"Love how Gonzaba Medical has stayed rooted in San Antonio's south side since Dr. Gonzaba started out."
— It's specific, references something real (history, roots, founder), and lands a genuine observation. It sounds like someone did 30 seconds of research, not like a template.

Another good example:
"Love how Woodlands Primary Health Care stands out as a real hub for comprehensive patient care in Spring."

Rules:
- Always start with "Love how [Company]..." — this is the consistent pattern.
- Follow with ONE specific thing: their specialty, niche, community they serve, history, approach, or what makes them stand out.
- End with a short, genuine observation — something that feels like a real take, not a compliment for the sake of it.
- If the city is natural at the end, add it. If not, leave it out.
- 15-20 words max. Short is better.
- Casual tone, contractions are fine.
- Do NOT mention: hiring, open roles, staffing.
- Do NOT use: em dashes (—), exclamation points, "truly", "really", "innovative", "passionate", "cutting-edge", "I noticed", "I came across", "I saw that".
- Use the CLEAN company name provided.
- Output the sentence only. Nothing else."""

USER_TEMPLATE = """Clean company name: {clean_company}
DM title: {dm_title}
Specialty / what they do: {company_desc}
Location: {location}
Job description excerpt: {job_desc}

Write the icebreaker."""


def safe_get(row, idx):
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_sheet_id_from_url(url):
    p = urlparse(url)
    if "docs.google.com" in p.netloc:
        parts = p.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service():
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


def tab_exists(service, sheet_id, title):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def ensure_columns(service, sheet_id, title, min_cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            gid = s["properties"]["sheetId"]
            have = s["properties"]["gridProperties"]["columnCount"]
            if have < min_cols:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"appendDimension": {
                        "sheetId": gid, "dimension": "COLUMNS",
                        "length": min_cols - have}}]},
                ).execute()
            return


def generate_icebreaker(client, row):
    clean_co = safe_get(row, COL_CLEAN_CO) or safe_get(row, COL_COMPANY)
    prompt = USER_TEMPLATE.format(
        clean_company=clean_co,
        dm_title=safe_get(row, COL_DM_TITLE) or "Practice Owner",
        location=safe_get(row, COL_LOCATION),
        company_desc=(safe_get(row, COL_COMPANY_DESC) or "(not available)")[:400],
        job_desc=(safe_get(row, COL_JOB_DESC) or "(not available)")[:400],
    )
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_tokens=120,
            temperature=0.7,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        # strip accidental quotes and replace em dashes with commas
        text = text.strip('"').strip("'").strip()
        text = text.replace("—", ",").replace("–", ",")
        return text
    except Exception as e:
        return f"[ERROR: {e}]"


def write_batch(service, sheet_id, updates):
    if not updates:
        return
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            return
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 503) and attempt < 3:
                time.sleep(4 * (2 ** attempt))
            else:
                raise


def main():
    ap = argparse.ArgumentParser(description="Generate personalized icebreakers")
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--preview", type=int, default=0,
                    help="Dry-run: generate and print N icebreakers without writing")
    ap.add_argument("--workers", type=int, default=LLM_WORKERS)
    args = ap.parse_args()

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)

    mode = f"PREVIEW ({args.preview})" if args.preview else "LIVE"
    print(f"=== Generate Icebreakers ({mode}) ===")
    print(f"Model: {AZURE_DEPLOYMENT}\n")

    total_written = total_skip = 0

    for tab in TABS:
        if not tab_exists(service, sheet_id, tab):
            continue

        ensure_columns(service, sheet_id, tab, COL_ICEBREAKER + 1)
        # write header
        if not args.preview:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
                    {"range": f"'{tab}'!{col_letter(COL_ICEBREAKER)}1",
                     "values": [["Icebreaker"]]}
                ]}).execute()

        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:AF10000"
        ).execute().get("values", [])

        # collect rows to process — one entry per COMPANY (dedupe), reuse across rows
        company_to_entry = {}
        for i, r in enumerate(rows):
            dm = safe_get(r, COL_DM_NAME)
            email = safe_get(r, COL_EMAIL)
            ice = safe_get(r, COL_ICEBREAKER)
            company = safe_get(r, COL_COMPANY)
            if dm and email and not ice and company:
                if company not in company_to_entry:
                    company_to_entry[company] = {"sheet_rows": [], "row": r, "company": company}
                company_to_entry[company]["sheet_rows"].append(i + 2)
        todo = list(company_to_entry.values())

        print(f"{tab}: {len(todo)} rows to generate  ({len(rows) - len(todo)} skipped)")

        if not todo:
            continue

        if args.preview:
            sample = todo[:args.preview]
            print(f"\nGenerating {len(sample)} previews...\n")
            for lead in sample:
                ice = generate_icebreaker(client, lead["row"])
                dm = safe_get(lead["row"], COL_DM_NAME)
                print(f"  [{lead['company'][:36]:36s}] {dm[:24]:24s}")
                print(f"  → {ice}")
                print()
            continue

        print(f"  {len(todo)} unique companies to generate for")

        # parallel generation, batch writes
        written = 0
        pending = []

        def run(lead):
            return lead, generate_icebreaker(client, lead["row"])

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run, lead): lead for lead in todo}
            done = 0
            for fut in as_completed(futs):
                lead, ice = fut.result()
                done += 1
                if not ice.startswith("[ERROR"):
                    # write same icebreaker to every row of this company
                    for rn in lead["sheet_rows"]:
                        pending.append({
                            "range": f"'{tab}'!{col_letter(COL_ICEBREAKER)}{rn}",
                            "values": [[ice]],
                        })
                    written += 1
                else:
                    print(f"  [!] {lead['company']}: {ice}")

                if len(pending) >= BATCH_SIZE or done == len(todo):
                    write_batch(service, sheet_id, pending)
                    pending = []
                    print(f"  {done}/{len(todo)} companies processed ({written} written)")
                    time.sleep(0.5)

        total_written += written
        print(f"  {tab} done: {written} icebreakers written\n")

    print(f"=== Done === total written: {total_written}")


if __name__ == "__main__":
    main()
