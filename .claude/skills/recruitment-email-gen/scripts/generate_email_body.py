"""
Generate email bodies for recruitment outreach (niche-agnostic).

The icebreaker stays static (from generate_icebreaker.py). The LLM's only job is to
extract TWO facts from each company's Description with Azure OpenAI GPT-5.1:
  icp    -> the SPECIFIC type of firms/employers they staff for
  roles  -> ONE specific role they place for that ICP (reused in both sentences)

The email copy is then assembled deterministically in code, so every fixed line
(icebreaker included) is guaranteed exact. Full email written to --col_body.
No subject line.

Assembled email:
  {static icebreaker}

  I stumbled across your work helping {icp} fill {roles}. Impressive stuff.

  I can connect you to a few HR managers and business leaders currently hiring for {roles}.

  Worth a quick chat?

  Best,
  Jude

Reads:  --col_company, --col_desc, --col_icebreaker, --col_status (optional filter)
Writes: --col_body (full assembled email)
Resume-safe: skips rows where the body cell is already populated.

Run:
  python3 -W ignore generate_email_body.py --sheet_url "URL" --tab "TAB" \
    --col_company A --col_desc H --col_icebreaker U \
    --col_status R --status_value found --col_body V [--preview N]
"""

import os
import re
import json
import time
import socket
import argparse
from dotenv import load_dotenv
from openai import AzureOpenAI, RateLimitError

socket.setdefaulttimeout(180)  # ride out transient network slowness on sheet reads/writes
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH   = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.1")

WRITE_BATCH = 10
MAX_RETRIES = 4
DESC_LIMIT  = 1500

# Static icebreaker fallback, used ONLY for --preview when no icebreaker column exists yet.
STATIC_ICEBREAKER = (
    "Hi {first_name},\n\n"
    "Love how you keep the human side front and center when sourcing candidates, "
    "not just letting AI do all the work. It seems you care more about fit than fill."
)

# Fixed body copy. icp/fill_phrase/hiring_phrase/buyer_phrase come from the LLM.
# fill_phrase reads as "fill ___" (roles form); hiring_phrase reads as "hiring for ___" (people form);
# buyer_phrase is the niche-appropriate decision-makers who hire that role.
BODY_TEMPLATE = (
    "I stumbled across your work helping {icp} fill {fill_phrase}. Impressive stuff.\n\n"
    "I can connect you with a few {buyer_phrase} currently hiring for {hiring_phrase}.\n\n"
    "Worth a quick chat?\n\n"
    "Best,\n"
    "Jude"
)

SYSTEM_PROMPT = (
    "You extract facts about a recruitment agency from its description, "
    "for use in a cold email. You respond in JSON only."
)

USER_PROMPT = """You are given a recruitment agency's name and description. Extract who they recruit for, to drop into a cold email.

Return JSON only: {{"icp": "...", "fill_phrase": "...", "hiring_phrase": "...", "buyer_phrase": "..."}}

Rules
1. icp = the SINGLE most relevant type of employer this agency staffs for. Exactly one, not a list. Specific, not generic. Examples: "schools", "civil construction firms", "aged care providers".
2. fill_phrase = ONE specific role they fill for that icp, phrased to read after "fill ___". Usually ends in "roles". One role only. Examples: "primary teacher roles", "site engineer roles", "registered nurse roles".
3. hiring_phrase = the SAME single role phrased as the people, to read after "hiring for ___". Examples: "primary teachers", "site engineers", "registered nurses".
4. buyer_phrase = the decision-makers who typically hire that role, phrased as people, matched to the niche. Examples: "school leaders and principals" for teachers, "hiring managers and business leaders" for corporate roles, "practice managers" for clinical roles, "site managers" for construction. Keep it short and plausible.
5. Base everything only on the description. Do not invent facts, locations, or clients that are not supported by it.
6. Keep each short, casual and plain. No fancy language. No em dashes. No exclamation points.

Company name: {company_name}
Company description: {description}
"""


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


def ensure_col(service, sheet_id, tab_name, c_body):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    current_cols = sheet["properties"]["gridProperties"]["columnCount"]
    needed_cols = c_body + 1
    if current_cols < needed_cols:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": needed_cols - current_cols,
            }}]},
        ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!{col_letter(c_body)}1",
        valueInputOption="RAW",
        body={"values": [["email_body"]]},
    ).execute()


def extract_icp_roles(client, company_name, description, retry=0):
    prompt = USER_PROMPT.format(
        company_name=company_name,
        description=(description or "")[:DESC_LIMIT],
    )
    try:
        message = client.chat.completions.create(
            model=AZURE_DEPLOYMENT,
            max_completion_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = (message.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        data = json.loads(text)
        icp     = (data.get("icp") or "").strip()
        fill    = (data.get("fill_phrase") or "").strip()
        hiring  = (data.get("hiring_phrase") or "").strip()
        buyer   = (data.get("buyer_phrase") or "").strip()
        if not icp or not fill or not hiring or not buyer:
            return None
        return icp, fill, hiring, buyer
    except RateLimitError:
        if retry < MAX_RETRIES:
            time.sleep((2 ** retry) * 2)
            return extract_icp_roles(client, company_name, description, retry + 1)
        return None
    except Exception as e:
        if retry < MAX_RETRIES:
            time.sleep((2 ** retry) * 2)
            return extract_icp_roles(client, company_name, description, retry + 1)
        print(f"  !! extraction failed: {e}", flush=True)
        return None


def assemble(icebreaker, icp, fill_phrase, hiring_phrase, buyer_phrase):
    body = icebreaker + "\n\n" + BODY_TEMPLATE.format(
        icp=icp, fill_phrase=fill_phrase, hiring_phrase=hiring_phrase, buyer_phrase=buyer_phrase)
    # Enforce Jude's no-em-dash rule regardless of model output.
    return body.replace("—", ",").replace("–", ",")


def flush(service, updates, sheet_id, tab_name, c_body):
    if not updates:
        return
    data = [
        {"range": f"'{tab_name}'!{col_letter(c_body)}{u['row']}", "values": [[u["body"]]]}
        for u in updates
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
    parser.add_argument("--col_company", default="A")
    parser.add_argument("--col_desc", default="H", help="Primary description column (e.g. web_summary)")
    parser.add_argument("--col_desc_fallback", default="", help="Fallback description column when primary is empty (e.g. Apollo H)")
    parser.add_argument("--col_icebreaker", default="U")
    parser.add_argument("--col_status", default="", help="Optional status column filter (blank = no filter)")
    parser.add_argument("--status_value", default="found")
    parser.add_argument("--col_body", default="V")
    parser.add_argument("--limit", type=int, default=0, help="Cap rows processed (test batches)")
    parser.add_argument("--preview", type=int, default=0, help="Generate + print N without writing")
    parser.add_argument("--preview_name", default="there", help="Placeholder first name for preview icebreaker")
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab

    c_comp = col_to_idx(args.col_company)
    c_desc = col_to_idx(args.col_desc)
    c_desc_fb = col_to_idx(args.col_desc_fallback) if args.col_desc_fallback else None
    c_ice  = col_to_idx(args.col_icebreaker)
    c_stat = col_to_idx(args.col_status) if args.col_status else None
    c_body = col_to_idx(args.col_body)
    status_value = args.status_value.strip().lower()

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

    service = get_service()
    if not args.preview:
        ensure_col(service, sheet_id, tab_name, c_body)

    last_col = col_letter(max(c_comp, c_desc, c_desc_fb or 0, c_ice, c_stat or 0, c_body))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]

    pending = []
    for i, row in enumerate(rows):
        if c_stat is not None:
            status = row[c_stat].strip().lower() if len(row) > c_stat else ""
            if status != status_value:
                continue
        company = row[c_comp].strip() if len(row) > c_comp else ""
        desc    = row[c_desc].strip() if len(row) > c_desc else ""
        if not desc and c_desc_fb is not None:
            desc = row[c_desc_fb].strip() if len(row) > c_desc_fb else ""
        ice     = row[c_ice].strip() if len(row) > c_ice else ""
        exists  = row[c_body].strip() if len(row) > c_body else ""
        if not company or exists:
            continue
        # In preview the sheet may not have an icebreaker column yet; synthesize one.
        if not ice:
            if not args.preview:
                continue
            ice = STATIC_ICEBREAKER.format(first_name=args.preview_name)
        pending.append({"row": i + 2, "company": company, "desc": desc, "icebreaker": ice})

    if args.limit:
        pending = pending[:args.limit]

    print("=== Generate Email Bodies ===\n")
    print(f"Rows to process: {len(pending)}\n")

    if args.preview:
        for p in pending[:args.preview]:
            res = extract_icp_roles(client, p["company"], p["desc"])
            if not res:
                print(f"--- Row {p['row']}  |  {p['company']}  ->  (no icp/roles extracted)\n")
                continue
            icp, fill, hiring, buyer = res
            print(f"--- Row {p['row']}  |  {p['company']}  |  icp='{icp}'  fill='{fill}'  hiring='{hiring}'  buyer='{buyer}' ---")
            print(assemble(p["icebreaker"], icp, fill, hiring, buyer))
            print()
        return

    updates = []
    for p in pending:
        res = extract_icp_roles(client, p["company"], p["desc"])
        if not res:
            continue
        icp, fill, hiring, buyer = res
        updates.append({"row": p["row"], "body": assemble(p["icebreaker"], icp, fill, hiring, buyer)})
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab_name, c_body)
            updates = []
    if updates:
        flush(service, updates, sheet_id, tab_name, c_body)

    print(f"\nDone - {len(pending)} rows processed.")


if __name__ == "__main__":
    main()
