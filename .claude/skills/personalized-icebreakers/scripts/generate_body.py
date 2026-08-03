"""
Phase 4 - assemble the full email body: icebreaker + offer, routed by niche.

The offer copy is Jude's template VERBATIM. Only two slots change per lead:
{icp} and {roles} ("{ICP} hiring for {roles they place}"). Everything else,
including the CTA and sign-off, is fixed copy and must never be reworded.
The only generation is the slot extraction: dossier niche_detail +
roles_placed -> "law firms" / "attorneys", validated and falling back to
generic values if it comes back weird.

Routing on the dossier's healthcare_fit (col U):
  primary / partial -> icp "healthcare employers"; roles "clinical staff" for
                       clinical niches, else slot-extracted (healthcare IT etc.)
  none              -> icp+roles slot-extracted from their actual niche
  unknown           -> generic ("employers" / "a range of roles")
  NOT_A_RECRUITER flag, no dossier, or NO ICEBREAKER -> skipped entirely
  (no-icebreaker rows are never sent: personalization-only experiment)

Greeting first names go through the shared nickname map (casualize-names rule).

Writes --col_body (full plain-text body incl. greeting, rides to Instantly as
{{personalization}} later) and --col_variant (healthcare/healthcare_partial/
niche/generic) for auditing the mix. Batch-of-10, resume-safe: skips rows
where --col_body is filled.

Run:
  python3 -W ignore generate_body.py --sheet_url "URL" --tab "TAB" \
    --col_first B --col_icebreaker L --col_niche S --col_niche_detail T \
    --col_hc_fit U --col_roles W --col_flags X --col_summary R \
    --col_body AB --col_variant AC [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from openai import AzureOpenAI
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
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "gpt-4.1")

WRITE_BATCH = 10
WORKERS     = 6

# ---------------------------------------------------------------------------
# COPY TEMPLATE - Jude's wording VERBATIM. {icp} and {roles} are the ONLY
# variables he authorized; never reword anything else in this block. Swap the
# whole template here for A/B tests, never treat current copy as permanent.
# ---------------------------------------------------------------------------

# Trimmed 2026-07-21 (Jude-approved) to land total emails in the 70-100 word
# band. Proof line is deliberately role-vague: the niche routing will not
# always be right, so no NPs/PAs or any role mention there.
BODY_CORE = (
    "I'm tracking {icp} hiring for {roles} right now, and a few are open "
    "to working with external recruiters.\n\n"
    "I'm not a recruiter myself, so I've been routing these to specialized "
    "recruiters who can deliver, hence why your name came up on my end.\n\n"
    "Last month, I delivered 12 qualified employer intros for a recruitment "
    "firm in TX.\n\n"
    "Are you open to new reqs right now, or already at capacity?\n\n"
    "Best,\nRood Judeley (Jude)"
)

ICP_HEALTHCARE  = "healthcare employers"
ROLES_CLINICAL  = "clinical staff"
ICP_GENERIC     = "employers"
ROLES_FALLBACK  = "a range of roles"

# Niches where a healthcare-primary agency really places clinical people.
# Healthcare IT / admin recruiters get slot-extracted roles instead.
CLINICAL_NICHES = ("healthcare_clinical", "generalist", "unknown", "")

# Slot extraction for the NICHE variant only: their vertical in plain words.
SLOT_SYSTEM = (
    "You are given research on a recruitment agency: their niche and the roles "
    "they place. Return STRICT JSON {\"employer_type\": \"...\", \"roles_phrase\": \"...\"}.\n"
    "employer_type: a plural noun phrase for the COMPANIES THAT HIRE those roles, "
    "as a person would say it in one breath: 'law firms', 'manufacturers', "
    "'logistics companies', 'accounting firms'. 1-3 words. NEVER words like "
    "staffing, recruiting, agencies (those are the agency, not their clients).\n"
    "roles_phrase: the roles in 1-4 words, plural, the broadest honest bucket: "
    "'attorneys', 'engineers', 'CNC machinists', 'finance people'. Prefer one "
    "role family over a list.\n"
    "Lowercase everything except acronyms (RNs, CNC, IT).\n"
    "If the research is too vague to be confident, return empty strings."
)

# Common nicknames only (casualize-names rule). Unlisted names pass through.
NICKNAMES = {
    "william": "Will", "robert": "Rob", "richard": "Rich", "michael": "Mike",
    "christopher": "Chris", "matthew": "Matt", "daniel": "Dan", "david": "Dave",
    "james": "Jim", "joseph": "Joe", "thomas": "Tom", "charles": "Charlie",
    "anthony": "Tony", "steven": "Steve", "stephen": "Steve", "andrew": "Andy",
    "kenneth": "Ken", "joshua": "Josh", "timothy": "Tim", "edward": "Ed",
    "jeffrey": "Jeff", "gregory": "Greg", "benjamin": "Ben", "samuel": "Sam",
    "patricia": "Pat", "jennifer": "Jen", "elizabeth": "Liz", "katherine": "Kate",
    "kathleen": "Kathy", "margaret": "Maggie", "deborah": "Deb", "rebecca": "Becca",
    "jacqueline": "Jackie", "alexandra": "Alex", "alexander": "Alex",
    "nicholas": "Nick", "jonathan": "Jon", "zachary": "Zach", "victoria": "Vicki",
    "pamela": "Pam", "cynthia": "Cindy", "sandra": "Sandy", "theodore": "Ted",
    "raymond": "Ray", "lawrence": "Larry", "ronald": "Ron", "donald": "Don",
    "douglas": "Doug", "frederick": "Fred", "leonard": "Len", "vincent": "Vince",
}


def casual_first(name):
    n = (name or "").strip().split()[0] if (name or "").strip() else ""
    return NICKNAMES.get(n.lower(), n.title() if n and n == n.lower() else n)


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


BANNED_SLOT_WORDS = ("staffing", "recruit", "agenc", "talent", "search firm")


def valid_slot(s, max_words):
    s = (s or "").strip()
    if not s or len(s.split()) > max_words:
        return False
    low = s.lower()
    return not any(b in low for b in BANNED_SLOT_WORDS)


def extract_slots(client, p):
    """NICHE variant only. Returns (employer_type, roles_phrase) or ('','')."""
    user = (f"niche: {p['niche']}\nniche detail: {p['niche_detail']}\n"
            f"roles placed: {p['roles']}\n"
            f"summary: {p['summary'][:600]}")
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=60, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SLOT_SYSTEM},
                      {"role": "user", "content": user}],
        )
        d = json.loads(resp.choices[0].message.content or "{}")
        et, rp = (d.get("employer_type") or "").strip(), (d.get("roles_phrase") or "").strip()
        # "it" comes back lowercase despite the prompt and reads as the pronoun.
        et = re.sub(r"\bit\b", "IT", et)
        rp = re.sub(r"\bit\b", "IT", rp)
        if valid_slot(et, 3) and valid_slot(rp, 4):
            # "manufacturers hiring manufacturing roles": if the slots share a
            # stem the roles half adds nothing, keep the employer type alone.
            et_stems = {w[:6] for w in et.lower().split() if len(w) > 5}
            if any(w[:6] in et_stems for w in rp.lower().split() if len(w) > 5):
                return et, ""
            return et, rp
        if valid_slot(et, 3):
            return et, ""
    except Exception:
        pass
    return "", ""


def assemble(first, icebreaker, icp, roles):
    body = BODY_CORE.format(icp=icp, roles=roles)
    parts = [f"Hi {casual_first(first)},", ""]
    if icebreaker:
        parts += [icebreaker, ""]
    parts.append(body)
    out = "\n".join(parts)
    return out.replace("—", ", ").replace("–", ", ")


def route(client, p):
    """Returns (variant, body). Fills only the {icp}/{roles} slots."""
    hc = p["hc"].lower()
    if hc in ("primary", "partial"):
        variant = "healthcare" if hc == "primary" else "healthcare_partial"
        if p["niche"].lower() in CLINICAL_NICHES:
            return variant, assemble(p["first"], p["ice"], ICP_HEALTHCARE, ROLES_CLINICAL)
        # healthcare recruiter but not clinical roles (healthcare IT, admin):
        # keep the healthcare ICP, extract what they actually place
        _, rp = extract_slots(client, p)
        return variant, assemble(p["first"], p["ice"], ICP_HEALTHCARE, rp or ROLES_FALLBACK)
    if hc == "none":
        et, rp = extract_slots(client, p)
        if et:
            return "niche", assemble(p["first"], p["ice"], et, rp or ROLES_FALLBACK)
        # vertical known to not be healthcare but too vague to name: generic
        return "generic", assemble(p["first"], p["ice"], ICP_GENERIC, ROLES_FALLBACK)
    return "generic", assemble(p["first"], p["ice"], ICP_GENERIC, ROLES_FALLBACK)


def ensure_cols(service, sheet_id, tab, cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sh = next(s for s in meta["sheets"] if s["properties"]["title"] == tab)
    need = max(c for c, _ in cols) + 1
    cur = sh["properties"]["gridProperties"]["columnCount"]
    if cur < need:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sh["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": need - cur}}]}).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab}'!{col_letter(c)}1", "values": [[h]]} for c, h in cols]}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--col_first", default="B")
    ap.add_argument("--col_icebreaker", default="L")
    ap.add_argument("--col_niche", default="S")
    ap.add_argument("--col_niche_detail", default="T")
    ap.add_argument("--col_hc_fit", default="U")
    ap.add_argument("--col_roles", default="W")
    ap.add_argument("--col_flags", default="X")
    ap.add_argument("--col_summary", default="R")
    ap.add_argument("--col_body", default="AB")
    ap.add_argument("--col_variant", default="AC")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--preview", type=int, default=0)
    a = ap.parse_args()

    sheet_id = parse_sheet_id(a.sheet_url)
    tab = a.tab
    C = {k: col_to_idx(v) for k, v in {
        "first": a.col_first, "ice": a.col_icebreaker, "niche": a.col_niche,
        "niche_detail": a.col_niche_detail, "hc": a.col_hc_fit, "roles": a.col_roles,
        "flags": a.col_flags, "summary": a.col_summary,
        "body": a.col_body, "variant": a.col_variant,
    }.items()}

    client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY,
                         api_version=AZURE_API_VERSION)
    service = get_service()
    if not a.preview:
        ensure_cols(service, sheet_id, tab,
                    [(C["body"], "email_body"), (C["variant"], "variant")])

    last = col_letter(max(C.values()))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:{last}").execute().get("values", [])[1:]

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    pending = []
    for i, r in enumerate(rows):
        if cell(r, C["body"]):
            continue
        if not cell(r, C["hc"]):
            continue                       # no dossier yet
        if "NOT_A_RECRUITER" in cell(r, C["flags"]):
            continue
        # Jude's experiment: personalization-only campaign. A row with no
        # icebreaker gets NO body and is never pushed, rather than a generic
        # open. Re-running after icebreakers improve picks these rows up.
        if not cell(r, C["ice"]):
            continue
        pending.append({
            "row": i + 2, "first": cell(r, C["first"]), "ice": cell(r, C["ice"]),
            "niche": cell(r, C["niche"]), "niche_detail": cell(r, C["niche_detail"]),
            "hc": cell(r, C["hc"]), "roles": cell(r, C["roles"]),
            "summary": cell(r, C["summary"]),
        })

    if a.limit:
        pending = pending[:a.limit]
    if a.preview:
        pending = pending[:a.preview]

    print(f"=== Generate Email Bodies ===\nRows with a dossier, no body yet: {len(pending)}\n")
    if not pending:
        return

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(route, client, p): p for p in pending}
        for fut in as_completed(futs):
            p = futs[fut]
            variant, body = fut.result()
            results.append((p["row"], p, variant, body))
    results.sort(key=lambda x: x[0])

    if a.preview:
        from collections import Counter
        for row, p, variant, body in results:
            print(f"--- Row {row}  [{variant}]  hc_fit={p['hc']}  niche={p['niche']} ---")
            print(body)
            print()
        print("=== VARIANT MIX ===")
        for k, v in Counter(v for _, _, v, _ in results).most_common():
            print(f"  {k:22s} {v}/{len(results)}")
        return

    updates = []
    for row, p, variant, body in results:
        updates.append((row, body, variant))
        if len(updates) >= WRITE_BATCH:
            _flush(service, sheet_id, tab, updates, C)
            updates = []
    if updates:
        _flush(service, sheet_id, tab, updates, C)
    from collections import Counter
    mix = Counter(v for _, _, v, _ in results)
    print(f"\nDone - {len(results)} bodies. Mix: {dict(mix)}")


def _flush(service, sheet_id, tab, updates, C):
    data = []
    for row, body, variant in updates:
        data.append({"range": f"'{tab}'!{col_letter(C['body'])}{row}", "values": [[body]]})
        data.append({"range": f"'{tab}'!{col_letter(C['variant'])}{row}", "values": [[variant]]})
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
            break
        except Exception as e:
            if attempt < 3 and "429" in str(e):
                time.sleep(65)   # 60 writes/min quota: back off, don't die
            else:
                raise
    print(f"  -> wrote {len(updates)} rows", flush=True)
    time.sleep(0.4)


if __name__ == "__main__":
    main()
