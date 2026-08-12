"""
Phase 3 (icebreaker research) - validate and write Claude-authored icebreaker
lines to the sheet. NO LLM call anywhere in this script.

Takes --verdicts FILE: a JSON list of {"row", "icebreaker", "fact_type"}
that I (Claude) wrote by hand after reading collect_icebreaker_research.py's
research packs. This script does NOT trust that input blindly — every line
still has to clear the same mechanical gates generate_icebreaker.py uses on
LLM output (is_sane / is_specific / is_surveillance), reused verbatim here as
a backstop against my own mistakes, not just a model's. A verdict for a row
that isn't in --candidates (the original research pack) is refused outright,
same hallucination guard as enrich_websites_exa.py's --verdicts flow.

--dry_run prints what would be written without touching the sheet (default
when --apply is not passed). Batch-of-10 writes when applying for real.

Run:
  python3 -W ignore apply_icebreakers.py \
    --sheet_url "URL" --tab Leads \
    --candidates data/icebreaker_batchN.json \
    --verdicts data/icebreaker_batchN_verdicts.json \
    [--apply] [--dry_run]
"""
import os
import re
import html
import json
import time
import argparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

WRITE_BATCH = 10
COL_ICEBREAKER = 31  # AF
COL_FACT_TYPE = 32   # AG

VALID_FACT_TYPES = {"founder_background", "published_numbers", "awards",
                    "milestone", "credentials", "niche", "none"}

# ---- Mechanical gates, reused verbatim from
# personalized-icebreakers/scripts/generate_icebreaker.py (Jude's locked v3
# formula rules). These ran as a backstop on LLM output there; here they run
# as a backstop on MY output, since Jude's instruction was zero-LLM
# end-to-end, not zero-verification. ----

BANNED_SUBSTRINGS = [
    "client trust", "clients trust", "clients stay", "builds trust",
    "people first", "quality over quantity", "genuine relationship",
    "tough to", "tough set", "hard to fill", "difficult market",
    "amazing", "incredible", "impress", "passionate",
    "love your website", "love your approach", "love what you're building",
    "love what you are building", "love your commitment", "love your take",
    "love your focus", "love your work",
]

STOPWORDS = {
    "the", "and", "you", "your", "that", "with", "from", "for", "have", "has",
    "kind", "real", "most", "firms", "firm", "years", "year", "work", "working",
    "before", "after", "into", "over", "their", "they", "this", "what", "when",
    "which", "while", "would", "about", "there", "here", "been", "being", "into",
    "staffing", "recruiting", "recruitment", "search", "talent", "hiring", "team",
    "production", "productions", "video", "videos", "creative", "studio", "studios",
}


def scrub(s):
    """Unescape HTML entities and normalize punctuation. The raw site-page
    text these lines are written from sometimes carries literal '&amp;' etc
    (source pages that were themselves double-encoded) — that leaked into
    finished lines twice in testing ('a&amp;e network', 'White &amp; Case')
    without any agent noticing, so this runs unconditionally before the
    gates, not just on flagged rows."""
    s = html.unescape(str(s or "")).strip()
    s = s.replace("—", ", ").replace("–", ", ")
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("…", "...")]:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


def is_specific(line, fact):
    if not line:
        return False
    if re.search(r"\d", line):
        return True
    for sent in re.split(r"(?<=[.?])\s+", line):
        for tok in sent.split()[1:]:
            if re.match(r"^[A-Z][a-zA-Z]{2,}", tok.strip(",.;:'\"")):
                return True
    lw = {w.strip(",.;:'\"").lower() for w in line.split()}
    fw = {w.strip(",.;:'\"").lower() for w in (fact or "").split()}
    shared = {w for w in (lw & fw) if len(w) > 4 and w not in STOPWORDS}
    return len(shared) >= 2


def is_surveillance(text):
    low = (text or "").lower()
    if re.search(r"\$\s?\d[\d,.]*\s*(?:k\b)?\s*(?:per|a|/)?\s*(?:week|hour|hr|day|shift)", low):
        return True
    if re.search(r"\b(?:ltd|limited|llc|inc)\b", low):
        return True
    return any(b in low for b in (
        "weekly rate", "hourly rate", "pay rate", "bill rate", "fee structure",
        "week assignment", "week contract", "per diem rate",
        "lifestyle notes", "lifestyle tips", "local tips", "travel tips",
        "things to do", "for travelers", "assignment spots",
        "assignment locations", "assignment location pages",
        "based in", "headquartered", "your hq", "registered in", "registered as",
    ))


def is_sane(line, first="", last=""):
    if not line:
        return False
    if len(line) < 30:
        return False
    if len(line.split()) > 60:
        return False
    if not line.rstrip().endswith((".", "?")):
        return False
    low = line.lower()
    # Jude, 2026-08-12: "Love that X" doesn't read naturally when X is a bare
    # client/brand-name listing ("Love that driven counts ford and crayola
    # among the brands you've worked with" — you can't "love" an inventory of
    # proper nouns). "Saw that X" / "Noticed that X" are valid alternate
    # openers for that case; "Love" stays the default for genuine
    # achievements/stories. The old blanket "starts with noticed -> reject"
    # was guarding against a truncated fragment ("Noticed you've"), not
    # banning "Noticed" as an opener — the length/punctuation checks above
    # already catch that fragment case, so it's safe to allow a well-formed
    # "Noticed that ..." opener through here.
    if not (low.startswith("love ") or low.startswith("saw ") or low.startswith("noticed ")):
        return False
    if any(b in low for b in BANNED_SUBSTRINGS):
        return False
    if "also btw" in low or low.rstrip(".").endswith("btw"):
        return False
    if any(w in low for w in ("lists itself", "categorises itself", "categorizes itself",
                              "listed under", "lists under", "classified as",
                              "listed as", "lists as", "industry listed",
                              "listed in the", "your listing")):
        return False
    if any(w in low for w in ("hadn't realized", "hadn't realised",
                              "didn't realize", "didn't realise",
                              "you were local", "a local shop", "fellow ",
                              "small world", "we've spoken", "we spoke",
                              "my neck of the woods", "near me", "close to me")):
        return False
    if any(w in low for w in ("undergrad", "bachelor", "your degree", "your mba",
                              "alma mater", "studied at", "poli sci", "did your degree")):
        return False
    if re.search(r"\b(?:you|she|he|they)\s+(?:started|began|worked)\s+(?:out\s+)?as\s+an?\s+"
                 r"(?:janitor|dishwasher|busboy|cashier|cleaner|waiter|waitress|bagger)",
                 low):
        return False
    if is_surveillance(line):
        return False
    if re.search(r"\bmust\b", low):
        return False
    # Locate the second half by its fixed formula marker ("Btw, also noticed/saw
    # how/that ..."), NOT by generic sentence-splitting on every period. Naive
    # splitting on r"(?<=[.?])\s+" breaks on abbreviation periods inside the
    # FIRST half ("Taraji P. Henson", "U.S. Small Business Administration"),
    # which shifts the real "Btw, also ..." clause out of sents[1] and false-
    # rejects a structurally correct line. Anchor on "btw" instead: it never
    # legitimately appears anywhere except introducing the second half.
    if "btw" in low:
        m = re.search(r"\bbtw,?\s+also\s+(?:noticed|saw)\s+(?:how|that)\b", low)
        if not m:
            return False
        head, tail = line[:m.start()], line[m.start():]
        formula_words = {"btw", "also", "noticed", "saw", "how", "that", "love"}

        def _anchors(s):
            out = set()
            for tok in s.split():
                t = tok.strip(",.;:'\"()")
                if (re.match(r"^[A-Z][a-zA-Z]{2,}", t) or re.search(r"\d", t)) \
                        and t.lower() not in formula_words:
                    out.add(t.lower())
            return out

        def _content(s):
            return {w.strip(",.;:'\"()").lower() for w in s.split()
                    if len(w.strip(",.;:'\"()")) > 4} - STOPWORDS - formula_words

        if not (_anchors(tail) - _anchors(head)) \
                and len(_content(tail) - _content(head)) < 3:
            return False
    for nm in (f"{first} {last}".strip(), first):
        if nm and len(nm) > 2 and nm.lower() in low:
            return False
    return True


# ---- end reused gates ----


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


def ensure_cols(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    need = COL_FACT_TYPE + 1
    current = sheet["properties"]["gridProperties"]["columnCount"]
    if current < need:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS", "length": need - current}}]}).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab_name}'!{col_letter(COL_ICEBREAKER)}1", "values": [["icebreaker"]]},
            {"range": f"'{tab_name}'!{col_letter(COL_FACT_TYPE)}1", "values": [["fact_type"]]},
        ]}).execute()


def flush(service, updates, sheet_id, tab_name):
    if not updates:
        return
    data = []
    for row, line, ftype in updates:
        data.append({"range": f"'{tab_name}'!{col_letter(COL_ICEBREAKER)}{row}", "values": [[line]]})
        data.append({"range": f"'{tab_name}'!{col_letter(COL_FACT_TYPE)}{row}", "values": [[ftype]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
    print(f"  -> wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet_url", required=True)
    ap.add_argument("--tab", default="Leads")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    with open(args.candidates) as f:
        candidates = json.load(f)
    with open(args.verdicts) as f:
        verdicts = json.load(f)

    by_row = {c["row"]: c for c in candidates}

    accepted, rejected, skipped_empty, hallucinated = [], [], 0, []
    for v in verdicts:
        row = v.get("row")
        line = scrub(v.get("icebreaker") or "")
        ftype = (v.get("fact_type") or "none").strip().lower()
        if ftype not in VALID_FACT_TYPES:
            ftype = "none"

        if row not in by_row:
            hallucinated.append(v)
            continue

        if not line:
            skipped_empty += 1
            accepted.append((row, "", "none"))
            continue

        pack = by_row[row]
        first, last = pack.get("first", ""), pack.get("last", "")

        # Build the fact-text corpus the line is allowed to draw specificity
        # from: linkedin about/experiences + all crawled page text. Loose on
        # purpose — this is a plausibility check, not a strict source match.
        li = pack.get("linkedin") or {}
        fact_pool = " ".join(filter(None, [
            li.get("about", ""), li.get("headline", ""),
            " ".join(e.get("title", "") + " " + e.get("sub", "") for e in li.get("experiences", [])),
            " ".join(pg.get("text", "") for pg in pack.get("site_pages", [])),
        ]))

        # A gate failure resolves to an empty cell, same as the agent choosing
        # "no icebreaker" itself — it must still be WRITTEN (not skipped), or
        # the row stays eligible forever and collect_icebreaker_research.py
        # re-collects it (and re-spends a subagent) on every future run.
        if not is_sane(line, first, last):
            rejected.append((row, line, "failed is_sane"))
            accepted.append((row, "", "none"))
            continue
        if not is_specific(line, fact_pool):
            rejected.append((row, line, "failed is_specific"))
            accepted.append((row, "", "none"))
            continue
        if is_surveillance(line):
            rejected.append((row, line, "failed is_surveillance"))
            accepted.append((row, "", "none"))
            continue

        accepted.append((row, line, ftype))

    print(f"=== Apply Icebreakers ===")
    print(f"Verdicts in file      : {len(verdicts)}")
    print(f"Hallucinated (no row) : {len(hallucinated)}")
    print(f"Rejected by gates     : {len(rejected)}")
    real_lines = sum(1 for _, l, _ in accepted if l)
    print(f"Empty (agent chose none)  : {skipped_empty}")
    print(f"Empty (gate-rejected)     : {len(rejected)}")
    print(f"Real icebreakers to write : {real_lines}")
    print(f"Total rows to write       : {len(accepted)}")

    if hallucinated:
        print("\n!! HALLUCINATED ROWS (not in candidates file, refused):")
        for v in hallucinated[:10]:
            print(f"   row={v.get('row')}")

    if rejected:
        print("\n!! REJECTED BY MECHANICAL GATES:")
        for row, line, reason in rejected[:15]:
            print(f"   row {row} [{reason}]: {line[:100]}")

    if not args.apply:
        print("\n(dry run — pass --apply to write accepted rows to the sheet)")
        return

    sheet_id = parse_sheet_id(args.sheet_url)
    tab = args.tab
    service = get_service()
    ensure_cols(service, sheet_id, tab)

    updates, batch = [], []
    for row, line, ftype in accepted:
        batch.append((row, line, ftype))
        if len(batch) >= WRITE_BATCH:
            flush(service, batch, sheet_id, tab)
            updates.extend(batch)
            batch = []
    if batch:
        flush(service, batch, sheet_id, tab)
        updates.extend(batch)

    written_real = sum(1 for _, l, _ in updates if l)
    print(f"\nDone - {len(updates)} rows written ({written_real} with a real icebreaker, "
          f"{len(updates) - written_real} left empty by design).")


if __name__ == "__main__":
    main()
