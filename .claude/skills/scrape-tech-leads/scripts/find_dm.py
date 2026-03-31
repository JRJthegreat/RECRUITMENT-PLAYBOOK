"""
Phase 1.5: Find decision makers for tech job postings via rules + Apify LinkedIn scraper.

Reads job postings from Google Sheets (both Perm and Contract tabs), determines the
target DM title using rules based on company size + role seniority (perm) or CTO-first
logic (contract), then finds the actual person on LinkedIn via Apify.
"""

import os
import sys
import json
import argparse
import time
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load .env from the skill's parent .claude directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
load_dotenv(ENV_PATH)

# Apify config
APIFY_LINKEDIN_ACTOR = "harvestapi~linkedin-company-employees"
APIFY_SYNC_BASE = "https://api.apify.com/v2/acts"
APIFY_TIMEOUT = 300

MAX_WORKERS = 5

# --- Seniority classification for the role being HIRED ---

SENIOR_TECH_TITLES = [
    "cto", "chief technology officer", "chief technical officer",
    "vp of engineering", "vp engineering", "vice president of engineering",
    "head of engineering", "head of technology",
    "director of engineering", "engineering director",
    "chief architect", "vp of technology",
    "vp of data", "head of data", "chief data officer", "cdo",
    "chief information officer", "cio", "chief ai officer",
    "director of technology", "director of software engineering",
]

MID_TECH_TITLES = [
    "engineering manager", "senior engineering manager",
    "staff engineer", "staff software engineer",
    "principal engineer", "principal software engineer",
    "solutions architect", "enterprise architect", "cloud solutions architect",
    "lead engineer", "tech lead", "architecture lead",
    "lead data engineer", "principal data engineer",
]

# --- Target title variations (what to search for on LinkedIn) ---

TARGET_CEO = [
    "CEO", "Chief Executive Officer", "Founder", "Co-Founder",
    "Owner", "President", "COO", "Chief Operating Officer",
    "Managing Partner", "General Manager", "Managing Director",
    "Algemeen Directeur", "Geschäftsführer", "Directeur Général",
]

TARGET_CTO = [
    "CTO", "Chief Technology Officer", "Co-Founder & CTO",
    "Chief Technology & Innovation Officer", "Chief Product and Technology Officer",
    "VP of Engineering", "VP Engineering", "Vice President of Engineering",
    "VP of Product & Technology", "VP of Product Engineering",
    "Chief Architect", "VP of Technology",
]

TARGET_VP_ENG = [
    "VP of Engineering", "VP Engineering", "Vice President of Engineering",
    "Head of Engineering", "SVP Engineering",
    "VP of Technology", "Head of Technology",
    "VP Software Engineering", "VP of Product Engineering",
    "VP of Product & Technology",
]

TARGET_DIRECTOR_ENG = [
    "Director of Engineering", "Engineering Director",
    "Director of Software Engineering", "Senior Director of Engineering",
    "Director of Technology", "Head of Software Development",
    "Head of Solution Engineering", "Head of Trading Engineering",
]

TARGET_ENG_MANAGER = [
    "Engineering Manager", "Senior Engineering Manager",
    "Software Engineering Manager", "Development Manager",
    "Technical Manager",
]

TARGET_HIRING = [
    "Head of HR", "HR Director", "Head of People",
    "VP of People", "VP People", "Chief People Officer",
    "Head of Talent", "Head of Talent Acquisition",
    "Talent Acquisition Manager", "Talent Acquisition Director",
    "HR Manager", "People Operations Manager",
    "Recruiting Manager", "Head of Recruiting",
]


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service(token_path):
    with open(token_path) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/spreadsheets"]),
    )
    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(token_path, "w") as f:
            json.dump(token_data, f)

    return build("sheets", "v4", credentials=creds)


def parse_employee_count(count_str):
    """Parse employee count range string to an integer (use upper bound)."""
    if not count_str:
        return None
    s = str(count_str).strip()
    s = s.replace(",", "").replace("+", "")
    parts = s.split("-")
    try:
        if len(parts) == 2:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def classify_job_seniority(job_title):
    """Classify the role being hired as senior, mid, or junior."""
    title_lower = (job_title or "").lower().strip()
    for keyword in SENIOR_TECH_TITLES:
        if keyword in title_lower:
            return "senior"
    for keyword in MID_TECH_TITLES:
        if keyword in title_lower:
            return "mid"
    return "junior"


def determine_target_titles_perm(job_title, employee_count_str):
    """
    Determine DM for PERM roles based on company size + role seniority.
    Returns (title_variations, target_level, confidence, reasoning).
    """
    seniority = classify_job_seniority(job_title)
    count = parse_employee_count(employee_count_str)

    # Rule 1: Hiring a senior tech leader → target CEO regardless of size
    if seniority == "senior":
        return (
            TARGET_CEO, "ceo", "high",
            f"Hiring senior tech leader ({job_title}), targeting CEO/Founder"
        )

    # Rule 2: Unknown company size → try CTO first, CEO fallback (handled in process_lead)
    if count is None:
        return (
            TARGET_CTO, "cto", "medium",
            f"Unknown company size, targeting CTO/VP Engineering first"
        )

    # Rule 3: Small company (<50 employees)
    if count < 50:
        return (
            TARGET_CEO, "ceo", "high",
            f"Small company ({count} employees), targeting CEO/Founder"
        )

    # Rule 4: Small-mid (50-200)
    if count <= 200:
        return (
            TARGET_CTO, "cto", "high",
            f"Small-mid company ({count} employees), targeting CTO/VP Engineering"
        )

    # Rule 5: Mid-size (200-1000)
    if count <= 1000:
        if seniority == "mid":
            return (
                TARGET_VP_ENG, "vp_eng", "high",
                f"Mid-size company ({count} employees), mid role ({job_title}), targeting VP/Head of Engineering"
            )
        # Junior role
        return (
            TARGET_VP_ENG + TARGET_DIRECTOR_ENG, "vp_or_director", "medium",
            f"Mid-size company ({count} employees), junior role ({job_title}), targeting VP Eng or Director Eng"
        )

    # Rule 6: Large (1000+)
    if seniority == "mid":
        return (
            TARGET_VP_ENG + TARGET_DIRECTOR_ENG, "vp_or_director", "medium",
            f"Large company ({count} employees), mid role ({job_title}), targeting VP Eng or Director Eng"
        )
    # Junior role at large company
    return (
        TARGET_DIRECTOR_ENG + TARGET_ENG_MANAGER, "director_or_manager", "medium",
        f"Large company ({count} employees), junior role ({job_title}), targeting Director Eng or Eng Manager"
    )




def search_linkedin_leaders(apify_token, company_linkedin_url):
    """Find all Director+ level employees at a company via Apify LinkedIn scraper."""
    url = f"{APIFY_SYNC_BASE}/{APIFY_LINKEDIN_ACTOR}/run-sync-get-dataset-items"

    payload = {
        "companies": [company_linkedin_url],
        "seniorityLevelIds": ["220", "300", "310", "320"],  # Director, VP, CXO, Owner/Partner
        "maxItems": 8,
        "profileScraperMode": "Short ($4 per 1k)",
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                params={"token": apify_token, "format": "json"},
                json=payload,
                timeout=APIFY_TIMEOUT,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            elif resp.status_code == 402:
                print(f"    Apify: insufficient credits")
                return []
            else:
                print(f"    Apify error {resp.status_code}: {resp.text[:200]}")
                if attempt == 0:
                    time.sleep(2)
        except requests.exceptions.Timeout:
            print(f"    Apify timeout (attempt {attempt + 1})")
            if attempt == 0:
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            print(f"    Apify request error: {e}")
            return []

    return []


def get_candidate_title(candidate):
    """Extract the current job title from an Apify LinkedIn result."""
    positions = candidate.get("currentPositions") or []
    if positions:
        return positions[0].get("title", "")
    return candidate.get("headline") or ""


def is_leadership_title(title):
    """Check if a title indicates someone with hiring authority, even if niche.

    Logic: hard reject → accept → soft reject → default reject.
    Hard rejects (assistant, secretary, retired, office of) fire first because
    "Personal Assistant CTO" is NOT a CTO even though it contains the word.
    """
    import re
    t = (title or "").strip()
    if not t:
        return False

    # --- HARD REJECT: person's actual role is clearly non-DM ---
    # These override everything — if you're an assistant, you're not a DM
    # even if CTO/CEO appears in your title (it's who you assist, not who you are)
    hard_reject = [
        r'\bassistant\b', r'\bsecretary\b',
        r'\bintern\b', r'\bstudent\b', r'\bfellow\b',
        r'\bcontractor\b', r'\bfreelance',
        r'\bresearcher\b', r'\bresearch associate\b', r'\bpostdoc', r'\blecturer\b',
        r'\bretired\b', r'\bin pension\b', r'\bformer\b',
        r'\boffice of\b',
        r'\bproduct owner\b', r'\bplatform owner\b', r'\bprocess owner\b', r'\bdata owner\b',
    ]
    # Only apply hard reject if the person's PRIMARY role (first segment) doesn't START with leadership
    primary = re.split(r'[,|;]', t)[0].strip()
    primary_is_leader = bool(re.match(
        r'(?:co-?)?(?:CEO|CTO|COO|CIO|CPO|VP|founder|director|head|managing director|'
        r'geschäftsführ|chief (?:executive|technology|operating|information|people|product|science))',
        primary, re.IGNORECASE
    ))
    # Always reject retired/pension regardless of title — a retired CEO is not a valid DM
    if re.search(r'\bretired\b|\bin pension\b|\bformer\b', t, re.IGNORECASE):
        return False

    if not primary_is_leader:
        for pat in hard_reject:
            if re.search(pat, t, re.IGNORECASE):
                return False
        # Also reject "chief of staff" only when it's the primary role
        if re.search(r'\bchief of staff\b', primary, re.IGNORECASE):
            return False

    # --- ACCEPT: tech/exec leadership ---
    accept = [
        r'\bCEO\b', r'\bCTO\b', r'\bCOO\b', r'\bCIO\b', r'\bCPO\b',
        r'\bchief\s+(?:[\w&]+\s+)+officer\b',  # Chief Technology Officer, Chief Product and Technology Officer, etc.
        r'\bfounder\b', r'\bco-founder\b',
        r'\bvp.{0,5}engineer', r'\bvice president.{0,5}engineer',
        r'\bsvp.{0,5}engineer', r'\bsenior vice president.{0,5}engineer',
        r'\bhead of engineer', r'\bhead of software', r'\bhead of technology\b',
        r'\bdirector of.{0,5}engineer', r'\bdirector of software',
        r'\bengineering director\b', r'\bengineering manager\b',
        r'\bsoftware engineering manager\b', r'\bsoftware development manager\b',
        r'\bgeschäftsführ', r'\balgemeen directeur\b',
        r'\bmanaging director\b',
        r'\bhr director\b', r'\bhr manager\b', r'\bhead of hr\b',
        r'\bhead of people\b', r'\bhead of talent\b', r'\btalent acquisition\b',
        r'\bowner\b', r'\bpresident\b', r'\bpartner\b',
        r'\bgeneral manager\b',
        r'\b(?:head|director|vp|vice president)\s+(?:of\s+)?',
        r'\bengineering (?:lead)\b',
        r'\b(?:senior\s+)?(?:engineering|software|development|technical) manager\b',
        r'\bdirecteur\b', r'\bdirekteur\b',
    ]
    for pat in accept:
        if re.search(pat, t, re.IGNORECASE):
            # Matched a leadership pattern — but check for soft rejects
            # Only check the role part (before @, //, etc.) not company name
            role_part = re.split(r'\s*(?://|@)\s*', t)[0].strip()
            soft_reject = [
                r'business development', r'client development', r'learning.{0,5}development',
                r'product development manager',
                r'\bsales\b', r'\baccount manager\b', r'\bpresales\b',
                r'\bmarket research\b',
                r'\bvice president business\b',
            ]
            for sr in soft_reject:
                if re.search(sr, role_part, re.IGNORECASE):
                    return False
            return True

    return False


def pick_best_match(candidates, title_variations):
    """Pick the best candidate from Apify results based on title priority."""
    if not candidates:
        return None, "low"

    import re

    # Pre-filter: remove candidates with non-DM titles before scoring.
    # "Personal Assistant CTO" contains \bCTO\b but the person is NOT the CTO.
    filtered = []
    for candidate in candidates:
        title = get_candidate_title(candidate)
        if is_leadership_title(title):
            filtered.append(candidate)

    if not filtered:
        return None, "low"

    # Build word-boundary regex patterns for exact title matching
    patterns = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE) for t in title_variations]

    scored = []
    for candidate in filtered:
        title = get_candidate_title(candidate)
        best_score = 999
        for i, pat in enumerate(patterns):
            if pat.search(title):
                best_score = min(best_score, i)
        scored.append((best_score, candidate))

    scored.sort(key=lambda x: x[0])
    best_score, best = scored[0]

    if best_score == 999:
        # No exact title match but is_leadership_title passed — return first as medium
        return filtered[0], "medium"
    elif best_score <= 2:
        return best, "high"
    else:
        return best, "medium"


def rank_candidate_for_lead(candidate, preferred_titles, fallback_order):
    """
    Score a candidate based on how well they match the preferred DM profile.
    Lower score = better match. Returns (score, candidate).

    preferred_titles: primary target titles (e.g. TARGET_CTO)
    fallback_order: list of (title_list, penalty) tuples for fallback tiers
    """
    import re
    title = get_candidate_title(candidate)
    if not is_leadership_title(title):
        return 9999  # Not a valid DM

    # Check primary target
    for i, t in enumerate(preferred_titles):
        if re.search(r'\b' + re.escape(t) + r'\b', title, re.IGNORECASE):
            return i  # Lower = better

    # Check fallback tiers
    base = len(preferred_titles)
    for tier_titles, penalty in fallback_order:
        for t in tier_titles:
            if re.search(r'\b' + re.escape(t) + r'\b', title, re.IGNORECASE):
                return base + penalty

    # Is a leader (passed is_leadership_title) but no exact match — still usable
    return 500


def process_lead(lead, apify_token):
    """Process a single lead: single Apify call for all leaders → pick best match locally."""
    tab = lead.get("tab", "Data")

    # Determine preferred target based on rules
    titles, level, base_confidence, reasoning = determine_target_titles_perm(
        lead["job_title"], lead["employee_count"]
    )

    # Build fallback order: primary target → CEO → CTO → VP → Director → HR
    fallback_order = []
    if level != "ceo":
        fallback_order.append((TARGET_CEO, 100))
    if level != "cto":
        fallback_order.append((TARGET_CTO, 200))
    if level not in ("vp_eng", "vp_or_director"):
        fallback_order.append((TARGET_VP_ENG, 300))
    if level not in ("director_or_manager", "vp_or_director"):
        fallback_order.append((TARGET_DIRECTOR_ENG, 400))
    fallback_order.append((TARGET_HIRING, 600))

    candidates = []
    method = "linkedin"

    if lead["company_linkedin_url"]:
        candidates = search_linkedin_leaders(
            apify_token, lead["company_linkedin_url"]
        )
    else:
        method = "no_linkedin_url"

    # Score all candidates and pick the best
    match = None
    confidence = "low"

    if candidates:
        scored = []
        for c in candidates:
            score = rank_candidate_for_lead(c, titles, fallback_order)
            if score < 9999:
                scored.append((score, c))

        if scored:
            scored.sort(key=lambda x: x[0])
            best_score, match = scored[0]

            if best_score < len(titles):
                confidence = "high"
                reasoning += f" | Primary match (score {best_score})"
            elif best_score < 200:
                confidence = "high"
                reasoning += f" | CEO fallback match"
            elif best_score < 400:
                confidence = "medium"
                reasoning += f" | CTO/VP fallback match"
            elif best_score < 600:
                confidence = "medium"
                reasoning += f" | Director fallback match"
            elif best_score < 700:
                confidence = "medium"
                reasoning += f" | HR fallback match"
            else:
                confidence = "medium"
                reasoning += f" | Leadership match (non-exact)"

    # Combine base confidence with match confidence
    conf_order = {"high": 3, "medium": 2, "low": 1}
    final_confidence = min(conf_order.get(base_confidence, 2), conf_order.get(confidence, 1))
    conf_labels = {3: "high", 2: "medium", 1: "low"}
    confidence = conf_labels[final_confidence]

    if match:
        first = match.get("firstName", "")
        last = match.get("lastName", "")
        person_name = f"{first} {last}".strip()
        result_title = get_candidate_title(match)
        linkedin_url = match.get("linkedinUrl", "")
        reasoning += f" | Found via {method}: {person_name} ({result_title})"
    else:
        person_name = ""
        result_title = ""
        linkedin_url = ""
        confidence = "low"
        reasoning += f" | No match found via {method}"

    return {
        "tab": tab,
        "row_num": lead["row_num"],
        "person_name": person_name,
        "result_title": result_title,
        "linkedin_url": linkedin_url,
        "dm_confidence": confidence,
        "dm_reasoning": reasoning,
    }


def col_letter(idx):
    """Convert 0-based column index to sheet letter (0=A, 25=Z, 26=AA, etc.)."""
    if idx < 26:
        return chr(65 + idx)
    return chr(64 + idx // 26) + chr(65 + idx % 26)


def read_tab_leads(service, sheet_id, tab_name, limit=0):
    """Read leads from a tab that need DM lookup. Returns (leads_list, col_indices)."""
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab_name}'"
    ).execute()
    all_rows = result.get("values", [])
    if len(all_rows) < 2:
        return [], {}

    headers = all_rows[0]

    def col_idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    indices = {
        "person_name": col_idx("person_name"),
        "result_title": col_idx("result_title"),
        "linkedin_url": col_idx("linkedin_url"),
        "company_name": col_idx("company name"),
        "job_title": col_idx("job_title"),
        "company_linkedin_url": col_idx("company_linkedin_url"),
        "company_url": col_idx("company_url"),
        "employee_count": col_idx("company_employee_count"),
        "dm_confidence": col_idx("dm_confidence"),
        "dm_reasoning": col_idx("dm_reasoning"),
    }

    # Validate required columns
    missing = []
    for name in ["person_name", "company_name", "job_title", "dm_confidence"]:
        if indices[name] is None:
            missing.append(name)
    if missing:
        print(f"  Tab '{tab_name}': Missing columns: {', '.join(missing)}")
        return [], {}

    leads = []
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            if idx is None:
                return ""
            return row[idx].strip() if idx < len(row) and row[idx].strip() else ""

        # Skip if already has person_name OR already attempted (dm_confidence set)
        if cell(indices["person_name"]) or cell(indices.get("dm_confidence")):
            continue

        company_name = cell(indices["company_name"])
        job_title = cell(indices["job_title"])
        if not company_name or not job_title:
            continue

        leads.append({
            "tab": tab_name,
            "row_num": i,
            "company_name": company_name,
            "job_title": job_title,
            "company_linkedin_url": cell(indices["company_linkedin_url"]),
            "company_url": cell(indices["company_url"]),
            "employee_count": cell(indices["employee_count"]),
        })

        if limit and len(leads) >= limit:
            break

    return leads, indices


def main():
    parser = argparse.ArgumentParser(description="Find decision makers for tech job postings")
    parser.add_argument("--sheet_url", required=True, help="Google Sheets URL or ID")
    parser.add_argument("--tab", default="Data",
                        help="Tab to process (default: Data), or comma-separated names")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process per tab (0 = all)")
    parser.add_argument("--dry_run", action="store_true", help="Preview rules output without calling Apify")
    args = parser.parse_args()

    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token and not args.dry_run:
        print("Error: APIFY_API_TOKEN not set in .env")
        sys.exit(1)

    token_path = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
    if not os.path.exists(token_path):
        print(f"Error: Google OAuth token not found at {token_path}")
        sys.exit(1)

    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    sheet_id = get_sheet_id_from_url(args.sheet_url)
    service = get_google_service(token_path)

    # Determine which tabs to process
    tabs_to_process = [t.strip() for t in args.tab.split(",") if t.strip()]

    # Collect leads from all tabs
    all_leads = []
    all_indices = {}  # tab_name → indices dict
    for tab_name in tabs_to_process:
        print(f"\nReading '{tab_name}' tab...")
        leads, indices = read_tab_leads(service, sheet_id, tab_name, args.limit)
        if leads:
            all_leads.extend(leads)
            all_indices[tab_name] = indices
            with_linkedin = sum(1 for l in leads if l["company_linkedin_url"])
            print(f"  {len(leads)} leads to process ({with_linkedin} with LinkedIn URL)")
        else:
            print(f"  No leads need DM lookup")

    if not all_leads:
        print("\nNo leads need DM lookup across any tab")
        sys.exit(0)

    print(f"\nTotal: {len(all_leads)} leads across {len(tabs_to_process)} tab(s)")

    # Dry run: just show rules output
    if args.dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — Rules output (no Apify calls)\n")
        for lead in all_leads[:20]:
            titles, level, confidence, reasoning = determine_target_titles_perm(
                lead["job_title"], lead["employee_count"]
            )
            print(f"  Row {lead['row_num']}: {lead['company_name']}")
            print(f"    Hiring: {lead['job_title']} | Employees: {lead['employee_count'] or '?'}")
            print(f"    Target: {level} → {', '.join(titles[:4])}...")
            print(f"    Confidence: {confidence} | {reasoning}")
            print(f"    Method: {'LinkedIn seniority filter' if lead['company_linkedin_url'] else 'No LinkedIn URL'}")
            print()
        if len(all_leads) > 20:
            print(f"  ... and {len(all_leads) - 20} more")
        print(f"{'='*70}")
        sys.exit(0)

    # Process leads in batches of 10
    BATCH_SIZE = 10
    total_batches = (len(all_leads) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nProcessing {len(all_leads)} leads in batches of {BATCH_SIZE} ({total_batches} batches, {MAX_WORKERS} workers)...\n")
    results = []
    found = 0
    not_found = 0

    for batch_num in range(total_batches):
        batch_start = batch_num * BATCH_SIZE
        batch = all_leads[batch_start:batch_start + BATCH_SIZE]
        batch_results = []

        print(f"--- Batch {batch_num + 1}/{total_batches} ---")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_lead = {
                executor.submit(process_lead, lead, apify_token): lead
                for lead in batch
            }
            for future in as_completed(future_to_lead):
                lead = future_to_lead[future]
                try:
                    result = future.result()
                    batch_results.append(result)
                    results.append(result)
                    if result["person_name"]:
                        found += 1
                        print(f"  [{result['tab']}] Row {result['row_num']}: {result['person_name']} ({result['result_title'][:50]}) [{result['dm_confidence']}]")
                    else:
                        not_found += 1
                        print(f"  [{result['tab']}] Row {result['row_num']}: No match — {lead['company_name']} [{result['dm_confidence']}]")
                except Exception as e:
                    not_found += 1
                    print(f"  [{lead['tab']}] Row {lead['row_num']}: Error — {e}")
                    batch_results.append({
                        "tab": lead["tab"],
                        "row_num": lead["row_num"],
                        "person_name": "",
                        "result_title": "",
                        "linkedin_url": "",
                        "dm_confidence": "low",
                        "dm_reasoning": f"Error: {e}",
                    })
                    results.append(batch_results[-1])

        # Write this batch to sheet immediately — group by tab
        for tab_name in tabs_to_process:
            tab_results = [r for r in batch_results if r["tab"] == tab_name]
            if not tab_results or tab_name not in all_indices:
                continue

            indices = all_indices[tab_name]
            updates = []
            for r in tab_results:
                row = r["row_num"]
                updates.append({
                    "range": f"'{tab_name}'!{col_letter(indices['person_name'])}{row}",
                    "values": [[r["person_name"]]],
                })
                if indices["result_title"] is not None:
                    updates.append({
                        "range": f"'{tab_name}'!{col_letter(indices['result_title'])}{row}",
                        "values": [[r["result_title"]]],
                    })
                if indices["linkedin_url"] is not None:
                    updates.append({
                        "range": f"'{tab_name}'!{col_letter(indices['linkedin_url'])}{row}",
                        "values": [[r["linkedin_url"]]],
                    })
                if indices["dm_confidence"] is not None:
                    updates.append({
                        "range": f"'{tab_name}'!{col_letter(indices['dm_confidence'])}{row}",
                        "values": [[r["dm_confidence"]]],
                    })
                if indices["dm_reasoning"] is not None:
                    updates.append({
                        "range": f"'{tab_name}'!{col_letter(indices['dm_reasoning'])}{row}",
                        "values": [[r["dm_reasoning"]]],
                    })

            if updates:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()

        print(f"  → Written to sheet. Running total: {found} found, {not_found} not found\n")

    # Summary
    print(f"\n{'='*50}")
    print(f"Find DM Complete")
    print(f"  Decision makers found: {found}")
    print(f"  Not found: {not_found}")
    print(f"  Total processed: {found + not_found}")
    high = sum(1 for r in results if r["dm_confidence"] == "high")
    med = sum(1 for r in results if r["dm_confidence"] == "medium")
    low = sum(1 for r in results if r["dm_confidence"] == "low")
    print(f"  Confidence: {high} high, {med} medium, {low} low")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
