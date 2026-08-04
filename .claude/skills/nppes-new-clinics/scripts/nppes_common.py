"""
Shared helpers for the nppes-new-clinics skill: config loading, SQLite store,
NPPES download plumbing, and address/name normalization.

Not a pipeline phase — imported by the phase scripts in this directory.
"""
import http.client
import json
import os
import re
import sqlite3
import ssl
import urllib.request
import zipfile
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(SKILL_DIR, "config")
DATA_DIR = os.path.join(SKILL_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
DB_PATH = os.path.join(DATA_DIR, "nppes.db")

NPPES_BASE = "https://download.cms.gov/nppes/"

# macOS system python has no CA bundle; CMS + NUCC are public reads
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# 50 states + DC. Deliberately excludes territories (PR, VI, GU, AS, MP),
# military codes (AE, AA, AP), and foreign addresses — not campaign targets.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}


def load_settings():
    with open(os.path.join(CONFIG_DIR, "settings.json")) as f:
        return json.load(f)


def resolve_states(value):
    """settings/CLI states value -> set of state codes. 'ALL' = 50 states + DC."""
    if isinstance(value, str):
        value = [s.strip() for s in value.split(",") if s.strip()]
    if any(v.upper() == "ALL" for v in value):
        return set(US_STATES)
    return {v.upper() for v in value}


def load_allowlist():
    """Category dict only (for label/shortage lookups)."""
    with open(os.path.join(CONFIG_DIR, "taxonomy_allowlist.json")) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_denylist():
    with open(os.path.join(CONFIG_DIR, "taxonomy_allowlist.json")) as f:
        raw = json.load(f)
    return raw.get("_denylist", [])


def match_taxonomy(code, allowlist, denylist=()):
    """Volume-first matching: denylist prefix -> out; first category prefix
    match wins (config order matters — precise codes before broad prefixes);
    anything else falls into the catch_all category if one is defined.
    Returns (category_key, category_dict) or (None, None)."""
    if not code:
        return None, None
    for prefix in denylist:
        if code.startswith(prefix):
            return None, None
    catch = None
    for key, cat in allowlist.items():
        if cat.get("catch_all"):
            catch = (key, cat)
            continue
        for prefix in cat["codes"]:
            if code.startswith(prefix):
                return key, cat
    return catch if catch else (None, None)


def ensure_dirs():
    for d in (DATA_DIR, RAW_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)


def get_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS practices (
      npi TEXT PRIMARY KEY,
      org_name TEXT, dba_name TEXT,
      enumeration_date TEXT, last_update_date TEXT, certification_date TEXT,
      addr1 TEXT, addr2 TEXT, city TEXT, state TEXT, zip TEXT,
      practice_phone TEXT, practice_fax TEXT,
      mailing_addr1 TEXT, mailing_addr2 TEXT, mailing_city TEXT,
      mailing_state TEXT, mailing_zip TEXT,
      ao_first TEXT, ao_last TEXT, ao_title TEXT, ao_phone TEXT, ao_credential TEXT,
      taxonomy_code TEXT, taxonomy_category TEXT, taxonomy_label TEXT,
      is_subpart TEXT, parent_lbn TEXT, parent_tin TEXT, sole_proprietor TEXT,
      addr_key TEXT,
      first_seen_date TEXT, source_file TEXT,
      classification TEXT, score INTEGER,
      addr_is_novel INTEGER,
      name_similarity REAL, name_match_npi TEXT, name_match_name TEXT,
      solo_flag INTEGER, solo_reason TEXT,
      owner_site_count INTEGER,
      classified_at TEXT,
      domain TEXT, email TEXT, email_source TEXT, email_verified TEXT,
      outreach_channel TEXT,
      exported_at TEXT, contacted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS baseline_addresses (
      addr_key TEXT, npi TEXT, entity TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_baseline_addr ON baseline_addresses(addr_key);
    CREATE TABLE IF NOT EXISTS baseline_orgs (
      npi TEXT, state TEXT, name_norm TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_baseline_orgs_state ON baseline_orgs(state);
    CREATE INDEX IF NOT EXISTS idx_baseline_orgs_name ON baseline_orgs(name_norm);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS runs (run_at TEXT, script TEXT, summary TEXT);
    """)
    return conn


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def log_run(conn, script, summary):
    conn.execute("INSERT INTO runs (run_at, script, summary) VALUES (?, ?, ?)",
                 (datetime.now().isoformat(timespec="seconds"), script, summary))
    conn.commit()


def download(url, dest, label=None):
    """Stream a URL to dest with a progress line. Returns False on any failure.

    A payload only reaches the cache if it is complete AND a valid zip —
    truncated downloads and HTML error pages with HTTP 200 are discarded, so
    a bad fetch can never poison future runs.
    """
    label = label or os.path.basename(dest)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"  {label}: {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="\r")
        if (total and done != total) or not zipfile.is_zipfile(tmp):
            os.remove(tmp)
            print(f"  {label}: truncated/invalid download discarded "
                  f"({done}/{total or '?'} bytes)")
            return False
        os.replace(tmp, dest)
        print(f"  {label}: {done / 1e6:.1f} MB downloaded")
        return True
    except urllib.error.HTTPError as e:
        print(f"  {label}: HTTP {e.code}")
        return False
    except (OSError, http.client.HTTPException) as e:
        print(f"  {label}: download failed - {type(e).__name__}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def url_exists(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX):
            return True
    except Exception:
        return False


def parse_nppes_date(s):
    """MM/DD/YYYY -> date, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


# ---------- normalization ----------

_ADDR_TOKEN_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "ROAD": "RD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL",
    "PARKWAY": "PKWY", "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY", "TERRACE": "TER",
    "TRAIL": "TRL", "SQUARE": "SQ", "TURNPIKE": "TPKE", "FREEWAY": "FWY",
    "PLAZA": "PLZ", "CENTER": "CTR", "CENTRE": "CTR", "POINT": "PT",
    "CROSSING": "XING", "JUNCTION": "JCT", "MOUNTAIN": "MTN", "FORT": "FT",
    "SAINT": "ST",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

# Unit-designator WORDS are dropped but their numbers kept, so "STE 200",
# "SUITE 200", "# 200", and "UNIT 200" all normalize to "200" — same suite,
# same key — while suite 200 vs suite 300 stay distinct locations.
_UNIT_WORDS = {"STE", "SUITE", "APT", "APARTMENT", "UNIT", "RM", "ROOM",
               "FL", "FLOOR", "BLDG", "BUILDING", "NO", "NUM", "NUMBER"}


def normalize_address(addr1, addr2, zip_code):
    """Canonical key for one practice location: normalized street line(s) + zip5.

    Unit NUMBERS are kept — different suites in one medical office building
    are different locations, and collapsing them would flag every clinic in a
    busy medical building as non-novel.
    """
    text = f"{addr1 or ''} {addr2 or ''}".upper()
    text = re.sub(r"[.,#]", " ", text)
    text = re.sub(r"[^A-Z0-9 ]", "", text)
    tokens = [_ADDR_TOKEN_MAP.get(t, t) for t in text.split()
              if t not in _UNIT_WORDS]
    zip5 = re.sub(r"[^0-9]", "", zip_code or "")[:5]
    return " ".join(tokens) + "|" + zip5


_LEGAL_SUFFIXES = {
    "LLC", "PLLC", "INC", "PC", "PA", "CORP", "CORPORATION", "LLP", "LTD",
    "PLC", "LP", "CO", "COMPANY", "INCORPORATED", "LIMITED", "SC", "PSC",
}


def normalize_org_name(name):
    """Uppercase, strip punctuation and trailing legal suffixes."""
    text = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    tokens = [t for t in text.split() if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


# Common filler words that make bad fuzzy-match anchor tokens
COMMON_NAME_TOKENS = {
    "CLINIC", "CENTER", "CENTRE", "HEALTH", "MEDICAL", "CARE", "WELLNESS",
    "FAMILY", "GROUP", "SERVICES", "SERVICE", "ASSOCIATES", "PRACTICE",
    "THE", "OF", "AND", "FOR", "HEALTHCARE", "THERAPY", "COUNSELING",
    "BEHAVIORAL", "MENTAL", "COMMUNITY", "INSTITUTE", "PARTNERS", "SOLUTIONS",
    "HOUSE", "HOME", "NEW", "FIRST",
    # frequent specialty/marketing words — long but useless as anchors
    "PSYCHIATRY", "PSYCHIATRIC", "PSYCHOLOGY", "PSYCHOLOGICAL", "PEDIATRIC",
    "PEDIATRICS", "DERMATOLOGY", "MEDICINE", "PRIMARY", "URGENT", "SURGERY",
    "SURGICAL", "DENTAL", "INTERNAL", "WOMEN", "WOMENS", "CHILDREN",
    "CHILDRENS", "ADVANCED", "PREMIER", "ELITE", "INTEGRATIVE", "HOLISTIC",
    "COMPREHENSIVE", "REGIONAL", "VALLEY", "COUNTY", "PHYSICAL",
}

# Clinical credentials that mark a person-named solo entity when they appear in
# the org name (after legal-suffix stripping — so "SMITH DERM PA" the legal
# suffix never false-positives as Physician Assistant).
_CREDENTIAL_TOKENS = {
    "MD", "DO", "DDS", "DMD", "DPM", "OD", "PHD", "PSYD", "LCSW", "LICSW",
    "LPC", "LPCC", "LMHC", "LMFT", "MFT", "CRNA", "APRN", "DNP", "FNP",
    "PMHNP", "NP", "PAC", "DPT", "OTR", "OTD", "SLP", "BCBA", "LAC", "LMT",
    "RN", "CNM", "DC",
}


def detect_solo(org_name, ao_last):
    """Deterministic solo-PLLC signals. Returns (flag, reason) — reason '' if clean.

    1. Authorized official's surname appears in the org legal name
       ("BAILEY J. GREY LLC" / AO GREY).
    2. A clinical credential survives inside a short org name
       ("M MICHAEL MASSUMI MD PA").
    """
    norm = normalize_org_name(org_name)
    tokens = norm.split()
    ao = re.sub(r"[^A-Z]", "", (ao_last or "").upper())
    if len(ao) >= 3 and ao in tokens:
        return 1, "ao_surname_in_name"
    if len(tokens) <= 6 and any(t in _CREDENTIAL_TOKENS for t in tokens):
        return 1, "credential_in_name"
    return 0, ""


def pick_anchor_token(name_norm):
    """Longest distinctive token for candidate prefiltering, or None."""
    candidates = [t for t in name_norm.split()
                  if len(t) >= 4 and t not in COMMON_NAME_TOKENS]
    return max(candidates, key=len) if candidates else None


def resolve_indices(header, names):
    """Map wanted column names -> indices from the file's own header row.

    Raises loudly on a missing column — schema drift must never be silent.
    """
    pos = {h: i for i, h in enumerate(header)}
    missing = [n for n in names if n not in pos]
    if missing:
        raise SystemExit(f"NPPES schema drift — missing columns: {missing}")
    return {n: pos[n] for n in names}
