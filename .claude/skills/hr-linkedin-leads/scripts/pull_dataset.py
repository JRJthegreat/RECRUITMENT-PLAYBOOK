"""
Shared schema and utilities for hr-linkedin-leads.

Field mapping based on insight_api_labs~linkedin-jobs-scraper output:
  id              → Job_Id
  title           → Job Title
  contractType    → Job Type
  sector          → Occupations
  publishedAt     → Date Published  (YYYY-MM-DD)
  salary          → Salary Min      (single string — LinkedIn doesn't split)
  applyUrl        → Apply URL
  description     → Job Description
  companyName     → Company Name
  companyUrl      → Company Website (LinkedIn company URL)
  (no size field) → Company Size    (blank — filled by find_company_sizes.py)
  benefits list   → Benefits
  location string → City, State     (parsed)
  jobUrl          → LinkedIn Job URL (column AC)
"""

import os
import re
import json
from urllib.parse import urlparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "..", "..", "..", "token.json")
load_dotenv(ENV_PATH)

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

BATCH_SIZE = 10
TAB_NAME = "Leads"
MAX_EMPLOYEES = 500
MIN_EMPLOYEES = 50

HEADERS = [
    # Job Info (A-J)
    "Job_Id",              # A — LinkedIn job id
    "Job Title",           # B
    "Job Type",            # C — contractType
    "Occupations",         # D — sector
    "Date Published",      # E — publishedAt
    "Salary Min",          # F — salary string (single field)
    "Salary Max",          # G — blank (schema compatibility)
    "Salary Period",       # H — blank
    "Apply URL",           # I — applyUrl
    "Job Description",     # J — description
    # Company (K-Q)
    "Company Name",        # K — companyName
    "Company Website",     # L — companyUrl (LinkedIn company page)
    "Company Size",        # M — blank at ingest, filled by find_company_sizes.py
    "Revenue",             # N — blank
    "CEO Name",            # O — blank
    "Company Description", # P — blank
    "Benefits",            # Q — benefits list joined
    # Location (R-S)
    "City",                # R — parsed from location
    "State",               # S — parsed from location
    # Outreach (T-AA) — blank, filled by downstream phases
    "DM Name",             # T
    "DM Title",            # U
    "LinkedIn URL",        # V
    "Email",               # W
    "First Name",          # X
    "Last Name",           # Y
    "Email Body",          # Z
    "Added to Instantly",  # AA
    # Derived
    "Seniority",           # AB — derived from Job Title at ingest
    "LinkedIn Job URL",    # AC — jobUrl
]

_SENIOR_TITLE_PATTERNS = (
    "chro", "cpo", "chief people", "chief human resources", "chief talent",
    "vp ", "vp,", "vice president", "svp ", "svp,", "evp ", "evp,",
    "director", "head of", "head,", "head ,",
)
_MID_TITLE_PATTERNS = ("manager", " lead", "senior ", "principal ")


def classify_seniority(job_title):
    t = (job_title or "").lower().strip()
    if not t:
        return ""
    if any(p in t for p in _SENIOR_TITLE_PATTERNS):
        return "Senior"
    if any(p in t for p in _MID_TITLE_PATTERNS):
        return "Mid"
    return "Junior"


def parse_location(location_str):
    """Parse 'City, ST' into (city, state). Returns ('', '') if unparseable."""
    if not location_str:
        return "", ""
    parts = location_str.rsplit(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return location_str.strip(), ""


def parse_size_lower_bound(size_str):
    """Parse LinkedIn size ranges like '51-200 employees' or Indeed '11 to 50'.
    Returns lower bound as int, or None if unparseable."""
    if not size_str:
        return None
    s = str(size_str).strip().replace(",", "").replace("+", "").replace(" employees", "")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"[^\d\-].*$", "", s).strip()
    if not s:
        return None
    parts = s.split("-")
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return None


def map_to_row(item):
    benefits = item.get("benefits") or []
    benefits_str = ", ".join(benefits[:8]) if isinstance(benefits, list) else str(benefits)
    city, state = parse_location(item.get("location") or "")

    return [
        # Job Info (A-J)
        item.get("id", ""),
        item.get("title", ""),
        item.get("contractType", ""),
        item.get("sector", ""),
        (item.get("publishedAt") or "")[:10],
        item.get("salary", ""),
        "",  # Salary Max — not available
        "",  # Salary Period — not available
        item.get("applyUrl", ""),
        item.get("description", ""),
        # Company (K-Q)
        item.get("companyName", ""),
        item.get("companyUrl", ""),
        "",  # Company Size — not available from LinkedIn actor
        "",  # Revenue
        "",  # CEO Name
        "",  # Company Description
        benefits_str,
        # Location (R-S)
        city,
        state,
        # Outreach (T-AA) — blank
        "", "", "", "", "", "", "", "",
        # Derived (AB-AC)
        classify_seniority(item.get("title", "")),
        item.get("jobUrl", ""),
    ]


def get_sheet_id_from_url(url):
    parsed = urlparse(url)
    if "docs.google.com" in parsed.netloc:
        parts = parsed.path.split("/")
        if "d" in parts:
            return parts[parts.index("d") + 1]
    return url


def get_google_service():
    with open(TOKEN_PATH) as f:
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
        with open(TOKEN_PATH, "w") as f:
            json.dump(token_data, f)
    return build("sheets", "v4", credentials=creds)
