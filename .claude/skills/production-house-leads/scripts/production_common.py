"""
Shared helpers for the production-house-leads skill: config loading, SQLite
store, and domain normalization.

Not a pipeline phase — imported by the phase scripts in this directory.

The store model mirrors nppes-new-clinics: scrape everything into
data/production.db, classify in place, then export campaign batches
(default 300) as Google Sheets with exported_at/batch_id stamps so the
same company is never worked twice.
"""
import json
import os
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(SKILL_DIR, "config")
DATA_DIR = os.path.join(SKILL_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "production.db")

# Hosts that are a social/portfolio page, not a company domain. The website
# cell keeps the URL (a Vimeo reel is still proof the studio is active) but
# domain stays NULL so email enrichment never runs against vimeo.com.
SOCIAL_HOSTS = {
    "facebook.com", "instagram.com", "vimeo.com", "youtube.com", "youtu.be",
    "linkedin.com", "linktr.ee", "twitter.com", "x.com", "tiktok.com",
    "behance.net", "squarespace.com", "wixsite.com", "wix.com",
    "business.site", "google.com", "yelp.com",
}


def load_settings():
    with open(os.path.join(CONFIG_DIR, "settings.json")) as f:
        return json.load(f)


def norm_domain(website):
    """Website URL -> registrable root domain, or None for social/portfolio hosts."""
    if not website:
        return None
    try:
        host = urlparse(website if "://" in website else "https://" + website).netloc.lower()
    except ValueError:
        return None
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    # root = last two labels (good enough for .com/.tv/.co; ccTLD SLDs like
    # .co.uk get three)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac") and len(parts[-1]) == 2:
        root = ".".join(parts[-3:])
    else:
        root = ".".join(parts[-2:])
    if root in SOCIAL_HOSTS:
        return None
    return root


def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        place_id      TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        website       TEXT,
        domain        TEXT,
        phone         TEXT,
        street        TEXT,
        city          TEXT,
        state         TEXT,
        postal        TEXT,
        country       TEXT,
        metro         TEXT,
        category      TEXT,
        categories    TEXT,
        rating        REAL,
        reviews       INTEGER,
        maps_url      TEXT,
        search_term   TEXT,
        scraped_at    TEXT,
        classification TEXT,
        class_reason  TEXT,
        classified_at TEXT,
        size_est      TEXT,
        size_source   TEXT,
        exported_at   TEXT,
        batch_id      INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_companies_class ON companies(classification);
    CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
    CREATE INDEX IF NOT EXISTS idx_companies_export ON companies(exported_at);
    CREATE TABLE IF NOT EXISTS runs (run_at TEXT, script TEXT, summary TEXT);
    """)
    return conn


def log_run(conn, script, summary):
    conn.execute("INSERT INTO runs VALUES (?, ?, ?)",
                 (datetime.now().isoformat(timespec="seconds"), script, summary))
    conn.commit()
