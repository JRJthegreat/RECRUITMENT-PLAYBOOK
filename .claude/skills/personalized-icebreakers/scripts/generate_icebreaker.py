"""
Phase 3 - turn the research dossier into a one-line personalized icebreaker.

Jude's formula (v3, July 2026): "Love {specific 1}, {compliment tied to
specific 1}. Btw, also noticed/saw {specific 2}." The compliment bridges the
two facts so they don't sit disconnected; it must credit them for the first
fact and be safe if slightly wrong. Facts come from the dossier's best_fact /
second_fact / research summary. No word limit. The line must read as if Jude
typed it after five minutes on their site: rounded numbers, sentence
capitalisation but company names and acronyms deliberately lowercase.

Per row: n=3 candidates at temp 0.8 -> mechanical gates (is_sane, is_specific)
-> temperature-0 reviewer with HARD/STYLE severity tiers. A HARD rejection
feeds the reviewer's notes into a revision call (max 2) instead of blind
resampling. Never converged = empty cell; Jude's rule is that no icebreaker
beats a sloppy one, so there is NO static fallback by default.

Falls back to the raw web_facts JSON slots (scrape_facts.py v1 output) only
when a row has no dossier.

Writes: --col_icebreaker (the line or empty), --col_fact_type (fact slot used,
or "none"). Batch-of-10 writes. Resume-safe: skips filled icebreaker cells and
NOT_A_RECRUITER-flagged rows. NOTE: --limit counts PENDING rows, not sheet rows.

Run:
  python3 -W ignore generate_icebreaker.py --sheet_url "URL" --tab "TAB" \
    --col_facts K --col_icebreaker L --col_fact_type M \
    --col_first B --col_last C --col_title G --col_company D \
    --col_best_fact V --col_summary R --col_second_fact Y \
    --col_best_when Z --col_second_when AA --col_flags X \
    [--limit N] [--preview N]
"""

import os
import re
import json
import time
import argparse
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

# Priority order. First non-empty slot wins.
# niche sits near the bottom on purpose: in testing it produced filler
# ("that's a space where depth really matters") rather than an observation.
FACT_PRIORITY = ["founder_background", "published_numbers", "awards",
                 "milestone", "credentials", "niche"]

# No static fallback. Jude's rule: no icebreaker beats a sloppy one, so a row
# with nothing concrete gets an empty cell and the body template omits the line.
DEFAULT_STATIC = ""

ICEBREAKER_SYSTEM = (
    "You write the opening one or two lines of a cold email to the owner of a "
    "recruitment agency. You spent five minutes on their site and LinkedIn and "
    "are typing a quick note about what stood out. Never marketing language.\n\n"
    "STRUCTURE (Jude's formula, follow it exactly)\n"
    "Love {specific 1}, {short compliment tied to specific 1}. Btw, also "
    "noticed how/that {specific 2 as a full clause about them}.\n"
    "The compliment is the connective tissue: it reacts to the first fact so "
    "the second fact doesn't sit next to it disconnected. Without it, 'Love "
    "X. Also Y.' reads like two index cards. The compliment must CREDIT THEM "
    "for the first fact, stay safe if slightly wrong (a general truth about "
    "why that fact is impressive, never a guess about their situation), and "
    "never speculate, never frame anything as a burden.\n"
    "The second half ALWAYS opens 'Btw, also noticed' or 'Btw, also saw', "
    "followed by 'how' or 'that' (pick whichever fits the sentence), then a "
    "complete clause about them:\n"
    "  'Btw, also noticed how you tie every job order to a donation'\n"
    "  'Btw, also saw that you offer founder advising alongside exec search'\n"
    "The how/that is mandatory: a bare noun phrase ('Btw, also saw the "
    "donations to...') stops reading like a human thought. Alternate the "
    "verb between 'noticed' and 'saw' across rows.\n"
    "Specific 2 must be a genuinely different fact. If only one genuine "
    "specific exists, write the first half alone and stop.\n\n"
    "WHY GENERATED TEXT GETS DETECTED, AND WHAT TO DO INSTEAD\n"
    "1. SYMMETRY. Two grammatically matched clauses read templated. Make the "
    "halves different lengths and different shapes: the compliment half and "
    "the btw half must not mirror each other.\n"
    "2. VERB MONOCULTURE. The second-half verb is 'noticed' or 'saw'; "
    "alternate between them across the batch (you will be told which one "
    "the previous rows used).\n"
    "3. UNIFORM POLISH. Humans are inconsistently casual. An occasional 'btw' "
    "or a fragment is right; the same tic in every line is wrong. Follow the "
    "provided register hint. 'btw' LEADS the second half: 'Btw, also "
    "noticed...', 'Btw, also saw...'. Never 'Also btw', never dangling at "
    "the end of a sentence.\n"
    "4. OVER-COMPLETENESS. Humans drop what context supplies. Do not restate "
    "the company name if it is already clear. Name two items where a machine "
    "would list three. Prefer possessive noun phrases over full clauses: "
    "'your ops background' not 'the fact that you worked in operations'.\n"
    "5. OVER-PRECISION. A human rounds and abbreviates: 'since 84', '20-odd "
    "years', 'Philly'. Exact-sounding recitation reads scraped.\n\n"
    "OPENER\n"
    "Every opener starts with 'Love': 'Love that you...', 'Love how you...', "
    "'Love seeing...'. No other opener exists (Jude's rule: other shapes "
    "drift into invented reactions). The Love must attach to the CONCRETE "
    "FACT, never to something vague. Perception verbs ('Saw you...') belong "
    "in the second half, not the opener. Follow the shape hint.\n"
    "The opener is the first sentence the reader sees after 'Hi {name},'. It "
    "must be a COMPLETE, ADDRESSED thought with a subject and a positive "
    "reaction. A subjectless note ('Went from AVP to starting Focus.') reads "
    "like private notes, not a message. Elision is for the second half only.\n"
    "WARMTH: at least one half must express a positive reaction to them. Two "
    "flat observations prove research but land cold.\n\n"
    "PICK THE NON-OBVIOUS DETAIL\n"
    "The homepage banner stat is what every lazy sender cites. Prefer the "
    "buried one: a named division, an association seat, an odd service line, "
    "a donation tied to their work, a second-generation detail. The reader "
    "should think nobody could have written this without actually looking.\n"
    "Both specifics must be facts ABOUT THEM: their background, their "
    "numbers, their longevity, things they built or won. Incidental website "
    "content (blog posts, travel tips, city lists, page features) is not a "
    "fact about them and produces empty flattery.\n"
    "Never cookie-cutter praise: 'love your website', 'love your approach', "
    "'love what you're building'.\n"
    "NEVER SURVEILLANCE MATERIAL. These read as monitoring, not research:\n"
    "- their live job listings, posted pay rates, fee structures\n"
    "- registered legal entity names, office addresses, HQ towns pulled "
    "from records\n"
    "- HOW A COMPANY CLASSIFIES ITSELF (directory category, industry label, "
    "how they list themselves)\n\n"
    "CAPITALISATION\n"
    "This mimics how a person types a quick email, not how a copywriter "
    "formats one:\n"
    "- Capitalise the first word of every sentence.\n"
    "- Capitalise PEOPLE'S names: Anthony, Frances Reitman, Scott.\n"
    "- Keep COMPANY and BRAND names lowercase: 'before starting volthire', "
    "'your careerspark program', 'at northwestern medicine'. People typing "
    "fast do not shift-key brand names, and correct branding everywhere is "
    "an instant AI tell.\n"
    "- Keep ACRONYMS lowercase: 'your shrm-cp', 'the emr systems', 'as an rn "
    "in the er', 'nyu coaching cert'.\n"
    "- Avoid opening a sentence with a company name or acronym so these "
    "rules never collide.\n\n"
    "SHORTEN NAMES\n"
    "Use the short company form you are given. Shorten locations too: "
    "'Philly', 'CT'.\n\n"
    "TIMELINE\n"
    "Each fact carries a when-tag. Only prior_career facts may use 'before', "
    "'prior to', 'went from'. A current_company fact is true today and must "
    "never be framed as the past. A dated_event keeps its date.\n\n"
    "OTHER PEOPLE\n"
    "Colleagues and founders other than the recipient may be MENTIONED as "
    "facts, never ASSESSED or characterised.\n\n"
    "HARD RULES\n"
    "- Address the recipient as 'you'. Never their name in third person.\n"
    "- Never education: degrees, universities, undergrad, study abroad.\n"
    "- Both specifics POSITIVE. Never frame their work or market as hard, "
    "slow, tough or competitive.\n"
    "- No claims about their clients or results. No values/culture/mission "
    "praise. No business-model commentary.\n"
    "- DIGNITY: no menial or student jobs, no personal hardship.\n"
    "- NEVER INVENT ANYTHING ABOUT THE SENDER. You know nothing about the "
    "person sending this email: not their location ('hadn't realized you were "
    "local'), not shared background ('fellow recruiter'), not past contact. "
    "Every claim must be about THEM, sourced from the research.\n"
    "- No em dashes, no exclamation marks, no semicolons. No word limit: "
    "take the space the two facts and the compliment need, but never pad.\n"
    "- If nothing is concrete enough, or only undignified material exists, "
    "reply with exactly SKIP. Empty beats filler.\n\n"
    "Return only the icebreaker text, nothing else."
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


def pick_facts(facts_json):
    """All non-empty slots in priority order, so a skipped fact can fall through
    to the next one instead of losing the row entirely."""
    try:
        facts = json.loads(facts_json) if facts_json else {}
    except Exception:
        return []
    if not isinstance(facts, dict):
        return []
    return [(s, (facts.get(s) or "").strip())
            for s in FACT_PRIORITY if (facts.get(s) or "").strip()]


def pick_fact(facts_json):
    """Back-compat single-slot helper."""
    got = pick_facts(facts_json)
    return got[0] if got else (None, "")


def scrub(s):
    s = (s or "").strip()
    s = s.replace("—", ", ").replace("–", ", ")
    # Normalize smart punctuation. GPT emits curly quotes inconsistently, which
    # looks wrong next to the straight quotes in the rest of the plain-text body.
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'),
                 ("”", '"'), ("…", "...")]:
        s = s.replace(a, b)
    s = s.strip('"').strip()
    s = re.sub(r"\s*,\s*,+", ", ", s)   # never ",," from dash swaps
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    # Mechanical safety net: models drift back to lowercase openers even when
    # told not to, so enforce sentence capitalisation here rather than relying
    # on the prompt alone.
    s = re.sub(r"(^|(?<=[.?])\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)
    return s


# Phrases the prompt bans. Cheap post-check: the model mostly obeys, but at
# thousands of rows "mostly" is not good enough and one flattering invention
# about their clients is worse than a static line.
BANNED_SUBSTRINGS = [
    "client trust", "clients trust", "clients stay", "builds trust",
    "people first", "quality over quantity", "genuine relationship",
    "tough to", "tough set", "hard to fill", "difficult market",
    "amazing", "incredible", "impress", "passionate",
    # Ban GENERIC love, not the formula itself. Saraev's rule is specific love.
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
}


def is_specific(line, fact):
    """Reject lines that could be sent to any agency.

    The whole point of an icebreaker is proof of research, so the line has to
    carry a hard specific. Accept if it contains a digit (a number, year, rate),
    or an interior capitalised word (a place, employer, award, credential), or
    it shares a distinctive content word with the researched fact.
    """
    if not line:
        return False
    if re.search(r"\d", line):
        return True
    # capitalised token that is not the first word of a sentence
    for sent in re.split(r"(?<=[.?])\s+", line):
        for tok in sent.split()[1:]:
            if re.match(r"^[A-Z][a-zA-Z]{2,}", tok.strip(",.;:'\"")):
                return True
    lw = {w.strip(",.;:'\"").lower() for w in line.split()}
    fw = {w.strip(",.;:'\"").lower() for w in (fact or "").split()}
    shared = {w for w in (lw & fw) if len(w) > 4 and w not in STOPWORDS}
    return len(shared) >= 2


def is_surveillance(text):
    """Records-scraping material: posted pay rates, assignment listings,
    registered entity names, HQ locations, incidental site content. Applied
    to the dossier second_fact BEFORE generation (the dossiers are already
    built, so bad facts must be filtered at use time) and to the finished
    line as a backstop. The LLM reviewer approved all of these in testing,
    so this cannot be prompt-only."""
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
    """Reject degenerate or wrong-person generations before they reach a sheet.

    Seen in testing: the model returned the two-word fragment "Noticed you've"
    with finish_reason='stop', and it referred to recipients in the third
    person ("Aaron Barnett brings ten years...") when the scraped fact carried
    their own name. Both would have shipped into live email.
    """
    if not line:
        return False
    if len(line) < 30:                      # fragment ("Noticed you've")
        return False
    if len(line.split()) > 60:              # no word limit (Jude's rule), but
        return False                        # a runaway ramble is still broken
    if not line.rstrip().endswith((".", "?")):   # cut mid-sentence
        return False
    low = line.lower()
    if not low.startswith("love "):         # Jude's rule: Love-formula openers
        return False                        # only; other shapes hallucinate
    if low.startswith("noticed"):           # robotic, banned in the prompt
        return False
    if any(b in low for b in BANNED_SUBSTRINGS):
        return False
    low2 = line.lower()
    if "also btw" in low2 or low2.rstrip(".").endswith("btw"):
        return False        # Jude's rule: 'Btw, also ...' leads; never 'Also btw' or trailing
    # Self-classification facts: the verifier has now missed this twice, so it
    # gets a mechanical ban rather than a third prompt reminder.
    if any(w in low2 for w in ("lists itself", "categorises itself", "categorizes itself",
                               "listed under", "lists under", "classified as",
                               "listed as", "lists as", "industry listed",
                               "listed in the", "your listing")):
        return False
    # Sender fabrications: the model inventing facts about JUDE (his location,
    # shared background, past contact). Seen: "hadn't realized you were local",
    # "nice to see a local shop". Nothing in the research supports any of it.
    if any(w in low2 for w in ("hadn't realized", "hadn't realised",
                               "didn't realize", "didn't realise",
                               "you were local", "a local shop", "fellow ",
                               "small world", "we've spoken", "we spoke",
                               "my neck of the woods", "near me", "close to me")):
        return False
    # Education keeps leaking in via the second half despite the prompt ban.
    if any(w in low for w in ("undergrad", "bachelor", "your degree", "your mba",
                              "alma mater", "studied at", "poli sci", "did your degree")):
        return False
    # Dignity: menial-first-job facts leak through dossier second_fact (the
    # janitor line shipped twice). Targeted pattern, not a bare word ban:
    # janitorial STAFFING is a legitimate niche, a janitor PAST is not a fact.
    if re.search(r"\b(?:you|she|he|they)\s+(?:started|began|worked)\s+(?:out\s+)?as\s+an?\s+"
                 r"(?:janitor|dishwasher|busboy|cashier|cleaner|waiter|waitress|bagger)",
                 low):
        return False
    if is_surveillance(line):
        return False
    # Second statement must be 'Btw, also noticed/saw how/that {clause}'.
    # A bare object ('Btw, also saw the donations...') fails: Jude's rule,
    # the how/that is what makes it read like a human thought.
    # Compliment speculation: 'must' is nearly always a guess about them
    # ("that range must give you a sharp read"), which Jude bans.
    if re.search(r"\bmust\b", low):
        return False
    sents = re.split(r"(?<=[.?])\s+", line.strip())
    if len(sents) > 1:
        tail = " ".join(sents[1:])
        if not re.match(r"btw,?\s+also\s+(?:noticed|saw)\s+(?:how|that)\b", tail.lower()):
            return False
        # The second half must ADD a new specific, not restate the first in
        # different words. New anchor = a proper noun or number not already
        # in the first half; failing that, at least 3 new content words.
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
        if not (_anchors(tail) - _anchors(sents[0])) \
                and len(_content(tail) - _content(sents[0])) < 3:
            return False
    # Third-person reference to the person being emailed.
    for nm in (f"{first} {last}".strip(), first):
        if nm and len(nm) > 2 and nm.lower() in low:
            return False
    return True


def casual_company(name):
    """Rough version of the casualize-names company rule: strip legal suffixes
    and generic staffing tails so the model gets a canonical short form instead
    of improvising one."""
    n = re.sub(r"[,.]", " ", name or "").strip()
    n = re.sub(r"\b(LLC|L\.L\.C|Inc|Incorporated|Ltd|Corp|Corporation|Co|Group|"
               r"Staffing|Search|Recruitment|Recruiting|Personnel|Solutions|"
               r"Services|Partners|Associates|Network|Resources|Agency|Firm|"
               r"International|Executive|Consulting)\b.*$", "", n, flags=re.I).strip(" ,-&")
    out = n if len(n) >= 3 else (name or "").strip()
    # Lowercase on purpose: Jude's rule is that quick-typed email does not
    # shift-key brand names, so the hint hands the model the casual form.
    return out.lower()


OPENER_SHAPES = ["love-that", "love-how", "love-seeing"]
# The formula fixes the register: second half always opens 'Btw, also
# noticed how/that ...' or 'Btw, also saw how/that ...'.
REGISTER_HINTS = ["second half opens 'Btw, also noticed how/that ...' or "
                  "'Btw, also saw how/that ...'"]


def extract_style_markers(line):
    """Perception verbs AND tail phrases from an emitted line, fed back as the
    avoid-list for subsequent rows. Tails matter as much as heads: the first
    batch converged on '... stood out.' four times. Openers are deliberately
    NOT tracked: every line starts with 'Love' by rule, so suppressing the
    opener bigram would fight the formula itself."""
    words = [w.strip(",.") for w in line.split()]
    verbs = [v for v in ("saw", "noticed") if v in line.lower()]
    tail = "ending '" + " ".join(words[-2:]).lower() + "'" if len(words) >= 2 else ""
    # 'Btw, also' itself is NOT tracked: it's the mandatory formula connector,
    # suppressing it would fight the structure. Only the verb rotates.
    return "", verbs + ([tail] if tail else [])


VERIFY_SYSTEM = (
    "You are a strict reviewer of cold-email opening lines. You get the "
    "recipient, the researched facts with when-tags, and candidate lines. "
    "Approve the best candidate that passes EVERY check, or reject all WITH "
    "actionable feedback so the writer can fix the draft.\n\n"
    "SEVERITY: checks are HARD or STYLE. A HARD failure forces choice=0 with "
    "feedback. A STYLE shortcoming must NOT cause rejection: choose the best "
    "candidate anyway and note the issue in feedback. Never zero a row over "
    "style.\n\n"
    "HARD CHECKS:\n"
    "1. FAITHFUL: every claim is supported by the given facts. NUMBERS get "
    "special scrutiny: every figure in the line must appear in the research "
    "or be a clearly stated rounding of one. A swapped number ('20th hire' "
    "when the research says 'second sales hire') fails.\n"
    "2. TIMELINE: 'before/prior/went from' only over prior_career facts. A "
    "current_company fact framed as past fails.\n"
    "3. STRUCTURE: the formula is 'Love {specific 1}, {compliment tied to "
    "specific 1}. Btw, also noticed/saw how/that {specific 2}.' The "
    "compliment must relate to the FIRST fact (a disconnected or generic "
    "compliment fails). The second half must introduce a NEW concrete "
    "detail: a different name, number, program, division, or offering. "
    "Restating the first half's fact in different words fails ('also "
    "noticed how you carried that experience forward when launching your "
    "own firm' adds nothing and fails). A single-half line (one specific + "
    "compliment) passes.\n"
    "4. NO ASSESSMENT OF THIRD PARTIES: naming a colleague as a fact is fine, "
    "characterising them fails.\n"
    "5. NO EDUCATION: degree/university/undergrad references fail.\n"
    "6. DIGNITY: menial or student jobs, personal hardship fail. 'Started "
    "as a janitor' is the canonical failure: it shipped twice because "
    "reviewers passed it. A menial past job is NEVER usable, no matter how "
    "admiring the framing.\n"
    "7. NO SPECULATION OR BURDEN FRAMING: guessing about their experience "
    "('you must have some stories') or framing their work as a load ('sounds "
    "like a lot to juggle') fails HARD.\n"
    "8. NO SELF-CLASSIFICATION FACTS: how the company lists or categorises "
    "itself is not usable material.\n"
    "9. OPENER: the first sentence must be a complete, addressed thought with "
    "a subject. A line starting with 'Also' fails.\n"
    "10. SECOND-HALF FORM: the second half must read 'Btw, also noticed "
    "how/that {clause about them}' or 'Btw, also saw how/that {clause}'. "
    "A bare noun phrase after the verb ('Btw, also saw the donations...') "
    "fails. 'Also btw' or a trailing 'btw' fails.\n"
    "11. NO SENDER FABRICATION: any claim about the SENDER (their location, "
    "'hadn't realized you were local', shared background, prior contact) "
    "fails. The research says nothing about the sender.\n"
    "12. NO SURVEILLANCE MATERIAL: quoting their live job listings, posted "
    "pay rates, fee structures, registered legal entity names, office "
    "addresses or HQ towns fails. Admiration built on records-scraping "
    "reads as monitoring.\n"
    "13. SUBSTANCE: both specifics must be facts about THEM (background, "
    "numbers, longevity, things built or won). A line built on incidental "
    "website content (blog topics, travel tips, city lists) fails.\n\n"
    "STYLE CHECKS (feedback only, never rejection):\n"
    "14. GENERIC FILLER: a half that could be sent to any agency.\n"
    "15. GENERATED-TEXT TELLS: parallel halves of near-equal length, company "
    "name restated, exhaustive lists, exact recitation where a human rounds.\n"
    "16. WARMTH: the compliment should feel genuine, not bolted on.\n"
    "17. FORM: sentence starts and PEOPLE'S names capitalised; company names "
    "and acronyms deliberately lowercase ('volthire', 'shrm-cp'), that is the "
    "intended style, never flag it. No em dashes or exclamation marks. No "
    "word limit. Informal register is desired, not a defect.\n\n"
    "SALVAGE: if a candidate's ONLY failure is a removable bad half, return "
    "the good half alone in 'trimmed', minimally reworded to open with "
    "'Love', properly capitalised, with a period. If it cannot honestly open "
    "with 'Love', do not salvage.\n\n"
    "FEEDBACK: when no candidate passes, list every violated check as a short "
    "imperative instruction ('replace the swapped number, the research says "
    "second sales hire', 'add a subject to the opener'). The writer will "
    "revise using exactly these notes.\n\n"
    "Return STRICT JSON: {\"choice\": 1-based index of the winner or 0, "
    "\"trimmed\": \"<salvaged line or empty>\", "
    "\"feedback\": [\"<instruction>\", ...]}"
)

MAX_REVISIONS = 2


def verify(client, user_context, candidates):
    """Return (approved_line, feedback_list). Empty line means rejection;
    the feedback then seeds the revision attempt."""
    listing = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=220, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": VERIFY_SYSTEM},
                      {"role": "user", "content": f"{user_context}\n\nCANDIDATES:\n{listing}"}],
        )
        d = json.loads(resp.choices[0].message.content or "{}")
        ch = int(d.get("choice", 0))
        if 1 <= ch <= len(candidates):
            return candidates[ch - 1], []
        trimmed = scrub(d.get("trimmed", ""))
        if trimmed and len(trimmed) >= 30 and trimmed.lower().startswith("love "):
            return trimmed, []
        fb = [str(f)[:160] for f in (d.get("feedback") or [])][:6]
        return "", fb
    except Exception:
        return "", []


def revise(client, user_context, prev_line, feedback):
    """One revision attempt driven by the reviewer's specific notes. This is
    the difference between a retry loop and a convergence loop: blind
    resampling draws from the same distribution, revision moves toward the
    stated objections."""
    notes = "\n".join(f"- {f}" for f in feedback)
    user = (f"{user_context}\n\nPREVIOUS ATTEMPT (rejected by the reviewer):\n"
            f"{prev_line}\n\nREVIEWER FEEDBACK, fix every point:\n{notes}\n\n"
            f"Write the corrected line.")
    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=90, temperature=0.4,
            messages=[{"role": "system", "content": ICEBREAKER_SYSTEM},
                      {"role": "user", "content": user}],
        )
        line = scrub(resp.choices[0].message.content)
        if line.strip().upper().startswith("SKIP"):
            return ""
        return line
    except Exception:
        return ""


def write_line(client, slot, fact, first="", last="", title="",
               company="", summary="", second="", bwhen="", swhen="",
               shape="love-that", register="clean", avoid=None, attempts=3):
    # The dossiers are already built and KEPT (Jude), so facts that violate
    # the surveillance rule must be neutralized at use time, not re-extracted.
    if is_surveillance(second):
        second = ""          # single-half line instead of a records-y second
    if is_surveillance(fact):
        return ""            # no clean primary fact -> no icebreaker
    user = (
        f"Writing to: {first} {last}".rstrip() +
        (f", {title}" if title else "") +
        (f", at {company}" if company else "") +
        (f" (short name: {casual_company(company)})" if company else "") +
        f"\nSuggested opening shape: {shape}" +
        f"\nRegister: {register}" +
        (f"\nAlready used in this batch's other emails, avoid: {', '.join(sorted(avoid))}" if avoid else "") +
        f"\n\nSPECIFIC 1 [{slot}, when={bwhen or 'unknown'}]: {fact}"
    )
    if second:
        user += f"\nSPECIFIC 2 (prefer this for the 'also' half, when={swhen or 'unknown'}): {second}"
    if summary:
        user += f"\n\nFULL RESEARCH (for context and extra specifics):\n{summary[:1400]}"

    def gate(line):
        return (line and not line.strip().upper().startswith("SKIP")
                and is_sane(line, first, last)
                and is_specific(line, fact + " " + second + " " + summary))

    try:
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=90, temperature=0.8, n=3,
            messages=[{"role": "system", "content": ICEBREAKER_SYSTEM},
                      {"role": "user", "content": user}],
        )
        raw = [scrub(ch.message.content) for ch in resp.choices]
        cands = list(dict.fromkeys(l for l in raw if gate(l)))
    except Exception:
        return ""
    if not cands:
        return ""

    # Verify-revise loop: rejection feedback seeds the next attempt instead of
    # blind resampling, so each round moves toward the reviewer's objections.
    for _ in range(1 + MAX_REVISIONS):
        line, feedback = verify(client, user, cands)
        if line:
            # Salvaged (trimmed) lines are NEW text that never passed the
            # mechanical gates; a bare-object second half shipped this way
            # once. Everything the reviewer returns gets gated.
            return line if gate(line) else ""
        if not feedback:
            return ""    # reviewer rejected with no fixable notes
        rev = revise(client, user, cands[0], feedback)
        if not gate(rev):
            return ""
        cands = [rev]
    return ""   # never converged; empty cell beats shipping a flagged line


def ensure_cols(service, sheet_id, tab_name, cols):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == tab_name)
    need = max(c for c, _ in cols) + 1
    current = sheet["properties"]["gridProperties"]["columnCount"]
    if current < need:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"appendDimension": {
                "sheetId": sheet["properties"]["sheetId"],
                "dimension": "COLUMNS",
                "length": need - current,
            }}]},
        ).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{tab_name}'!{col_letter(c)}1", "values": [[h]]} for c, h in cols
        ]},
    ).execute()


def flush(service, updates, sheet_id, tab_name, c_ice, c_type):
    if not updates:
        return
    data = []
    for row, line, ftype in updates:
        data.append({"range": f"'{tab_name}'!{col_letter(c_ice)}{row}", "values": [[line]]})
        data.append({"range": f"'{tab_name}'!{col_letter(c_type)}{row}", "values": [[ftype]]})
    for attempt in range(4):
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
            ).execute()
            break
        except Exception as e:
            # 60 writes/min/user quota: back off and retry instead of dying
            # mid-run and losing the batch.
            if attempt < 3 and "429" in str(e):
                time.sleep(65)
            else:
                raise
    print(f"  -> Wrote {len(updates)} rows", flush=True)
    time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet_url", required=True)
    parser.add_argument("--tab", required=True)
    parser.add_argument("--col_facts", default="AN")
    parser.add_argument("--col_icebreaker", default="AO")
    parser.add_argument("--col_fact_type", default="AP")
    parser.add_argument("--col_first", default="B", help="Recipient first name")
    parser.add_argument("--col_last", default="C", help="Recipient last name")
    parser.add_argument("--col_title", default="", help="Recipient job title (optional)")
    parser.add_argument("--col_company", default="", help="Company name (optional)")
    parser.add_argument("--col_best_fact", default="", help="Dossier best_fact column")
    parser.add_argument("--col_summary", default="", help="Dossier research_summary column")
    parser.add_argument("--col_second_fact", default="", help="Dossier second_fact column")
    parser.add_argument("--col_best_when", default="", help="best_fact_when column")
    parser.add_argument("--col_second_when", default="", help="second_fact_when column")
    parser.add_argument("--col_flags", default="", help="flags column; NOT_A_RECRUITER rows are skipped")
    parser.add_argument("--static_fallback", default=DEFAULT_STATIC,
                        help="Default empty: no icebreaker beats a sloppy one")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--preview", type=int, default=0, help="Generate + print N, write nothing")
    args = parser.parse_args()

    sheet_id = parse_sheet_id(args.sheet_url)
    tab_name = args.tab
    c_facts = col_to_idx(args.col_facts)
    c_ice   = col_to_idx(args.col_icebreaker)
    c_type  = col_to_idx(args.col_fact_type)
    c_first = col_to_idx(args.col_first)
    c_last  = col_to_idx(args.col_last)
    c_title = col_to_idx(args.col_title) if args.col_title else None
    c_comp  = col_to_idx(args.col_company) if args.col_company else None
    c_best  = col_to_idx(args.col_best_fact) if args.col_best_fact else None
    c_summ  = col_to_idx(args.col_summary) if args.col_summary else None
    c_sec   = col_to_idx(args.col_second_fact) if args.col_second_fact else None
    c_bw    = col_to_idx(args.col_best_when) if args.col_best_when else None
    c_sw    = col_to_idx(args.col_second_when) if args.col_second_when else None
    c_flag  = col_to_idx(args.col_flags) if args.col_flags else None

    client = AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY, api_version=AZURE_API_VERSION,
    )
    service = get_service()
    if not args.preview:
        ensure_cols(service, sheet_id, tab_name,
                    [(c_ice, "icebreaker"), (c_type, "fact_type")])

    last_col = col_letter(max(c_facts, c_ice, c_type, c_first, c_last,
                              c_title or 0, c_comp or 0, c_best or 0, c_summ or 0,
                              c_sec or 0, c_bw or 0, c_sw or 0, c_flag or 0))
    rows = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'!A:{last_col}"
    ).execute().get("values", [])[1:]

    def cell(row, idx):
        return row[idx].strip() if (idx is not None and len(row) > idx) else ""

    pending = []
    for i, row in enumerate(rows):
        if cell(row, c_ice):
            continue
        # Dossier best_fact is the ONLY generation source. Raw page_abstracts
        # (col K) are dossier INPUT, not icebreaker material: the {"pages":...}
        # format has no fact slots, so pre-dossier rows would burn LLM calls
        # and write nothing (this once swept 1,049 crawled-but-undossiered
        # rows). Rows gain eligibility when build_dossier.py fills best_fact.
        if not cell(row, c_best):
            continue
        # Not a recruitment agency at all (software vendor, consultancy).
        # They should not be in the campaign, so do not spend copy on them.
        if "NOT_A_RECRUITER" in cell(row, c_flag):
            continue
        pending.append({
            "row": i + 2,
            "facts": cell(row, c_facts),
            "first": cell(row, c_first),
            "last":  cell(row, c_last),
            "title": cell(row, c_title),
            "company": cell(row, c_comp),
            "best": cell(row, c_best),
            "summary": cell(row, c_summ),
            "second": cell(row, c_sec),
            "bwhen": cell(row, c_bw),
            "swhen": cell(row, c_sw),
        })

    if args.limit:
        pending = pending[:args.limit]

    print("=== Generate Icebreakers ===\n")
    print(f"Rows to generate: {len(pending)}\n")

    def build(p):
        """Prefer the dossier's chosen best_fact (synthesised from website +
        LinkedIn). Fall back to the raw website fact slots only if no dossier
        exists for the row yet."""
        shape = OPENER_SHAPES[p["row"] % len(OPENER_SHAPES)]
        register = REGISTER_HINTS[p["row"] % len(REGISTER_HINTS)]
        cands = []
        if p.get("best"):
            cands.append(("dossier", p["best"]))
        cands += pick_facts(p["facts"])
        for slot, fact in cands:
            line = write_line(client, slot, fact, p["first"], p["last"],
                              p["title"], p.get("company", ""), p.get("summary", ""),
                              p.get("second", ""), p.get("bwhen", ""), p.get("swhen", ""),
                              shape, register, p.get("avoid"))
            if line:
                return line, slot, fact
        return args.static_fallback, ("static" if args.static_fallback else "none"), ""

    if args.preview:
        pending = pending[:args.preview]
        counts = {}
        recent = []
        for p in pending:
            p["avoid"] = {m for mk in recent[-8:] for m in mk}
            line, ftype, fact = build(p)
            if line:
                op, vb = extract_style_markers(line)
                recent.append(([op] if op else []) + vb)
            counts[ftype] = counts.get(ftype, 0) + 1
            who = f"{p['first']} {p['last']}".strip()
            print(f"--- Row {p['row']}  [{ftype}]  to: {who} ---")
            if fact:
                print(f"  fact: {fact[:150]}")
            print(f"  {line if line else '(no icebreaker)'}")
            print()
        print("=== FACT TYPE MIX ===")
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {k:20s} {v}/{len(pending)}")
        return

    updates, done, personalized = [], 0, 0
    recent = []
    for p in pending:
        p["avoid"] = {m for mk in recent[-8:] for m in mk}
        line, ftype, _ = build(p)
        if line:
            op, vb = extract_style_markers(line)
            recent.append(([op] if op else []) + vb)
        if line:
            personalized += 1
        updates.append((p["row"], line, ftype))
        done += 1
        if len(updates) >= WRITE_BATCH:
            flush(service, updates, sheet_id, tab_name, c_ice, c_type)
            updates = []
        if done % 25 == 0:
            print(f"  ...{done}/{len(pending)} ({personalized} personalized)", flush=True)
    if updates:
        flush(service, updates, sheet_id, tab_name, c_ice, c_type)

    print(f"\nDone - {personalized}/{len(pending)} personalized, "
          f"{len(pending) - personalized} on the static fallback.")


if __name__ == "__main__":
    main()
