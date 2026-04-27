---
name: tech-leads-indeed
description: Pan-European tech recruitment lead pipeline. Orchestrates Apify Indeed scrapes (valig/indeed-jobs-scraper) across a keyword × EU-city grid with a 14-day filter, ingests to Google Sheets (≤500 employees), classifies out recruitment agencies, dedupes by company, finds decision makers via Google Search (CEO/CTO/HR routing), verifies them via LinkedIn profile scrape, enriches emails via AnyMail Finder (person + /decision-maker fallback), generates personalized outreach with Claude Opus 4.5, and pushes to Instantly. Use when the user asks to run the tech Indeed lead pipeline, or provides a pre-existing Apify dataset ID for ingestion only.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Tech Leads — Indeed Pipeline

## What This Skill Does

Generates pan-European tech recruitment leads from Indeed via Apify. Default flow: the skill runs the Apify `valig/indeed-jobs-scraper` actor across a keyword × city grid (last 14 days, ≤500 employees, pan-EU), classifies companies to drop recruitment agencies/job boards, dedupes by company, finds each company's domain, targets decision makers via Google Search (CEO / CTO / HR routing), verifies each DM is actually employed at the target company (LinkedIn profile scrape), enriches emails via AnyMail Finder (person endpoint when DM found, `/decision-maker` endpoint with category fallback when not), generates outreach with Claude Opus 4.5, and pushes to an Instantly campaign.

Manual override: `pull_dataset.py` still works for ingesting a dataset ID the user scraped by hand on Apify's web UI.

**Target roles** (typical Indeed scrape filter):
Backend Engineer, Frontend Engineer, Full Stack Engineer, DevOps Engineer, Data Engineer, Machine Learning Engineer, Site Reliability Engineer, Mobile Engineer, QA Engineer, Engineering Manager, Head of Engineering, CTO.

**Key differences from `civil-engineering-leads-indeed`:**
- Geography: pan-European tech hubs (London/Amsterdam/Berlin/Paris/Dublin/Stockholm/Munich/Zurich/etc.) — not UK-only
- No Perm/Contract filter (single template covers both)
- DM ops layer is **CTO / VP Engineering / Head of Engineering** (tech firms don't have a COO owning eng hiring)
- HR enters earlier — at **200+ employees** (tech firms professionalise TA much sooner than construction)

---

## Pipeline Phases

```bash
# Phase 1 — Scrape Apify Indeed (keyword × EU-city grid, 14-day filter, ≤500 employees)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" [--limit 100] [--days 14] [--workers 8] \
  [--keywords "A,B,..."] [--cities "A,B,..."] [--dry_run] [--yes]

# Phase 1 (manual fallback) — Pull a pre-existing Apify dataset ID
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/pull_dataset.py \
  --dataset_id "DATASET_ID" --sheet_url "URL" [--sheet_title "Title"] [--limit N]

# Phase 1.75 — Classify companies + delete agencies/job boards
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/classify_companies.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.8 — Dedupe by company (keep highest seniority; oldest Date Published wins ties)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/dedupe_by_company.py \
  --sheet_url "SHEET_URL" [--apply]

# Phase 1.9 — Populate official company domains (required before Phase 2)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/find_company_domains.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 2 — Find decision makers via Google Search + LinkedIn (CEO/CTO/HR routing)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/find_dm.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 2.5 — Verify DMs actually work at target company (drops false positives)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/verify_dms.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 3 — Enrich emails via AnyMail Finder (two-mode: person + /decision-maker)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/enrich_emails.py \
  --sheet_url "SHEET_URL" [--limit N] [--retry_not_found] [--dry_run]

# Phase 4 — Generate emails (MUST get user approval on TEMPLATE first)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/generate_emails.py \
  --sheet_url "SHEET_URL" [--preview N] [--overwrite] [--limit N]

# Phase 5 — Push to Instantly campaign (new or existing via --campaign_id)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_name "NAME" [--campaign_id "ID"] [--dry_run]
```

---

## DM Targeting Rules (Phase 2)

| Company Size | Job Being Hired | Pass 1 Target | Pass 2 (auto-retry) |
|---|---|---|---|
| <50 | Any | CEO / Founder / Owner | CTO / VP Engineering |
| 50–200 | Any | CTO / VP Engineering / Head of Engineering | CEO / Founder |
| 200–500 | C-level role (CTO, CFO, CEO, VP Eng, Engineering Director) | CEO / Founder | CTO |
| 200–500 | Any other eng / IC role | HR Director / TA Director / Head of People | CTO / VP Engineering |
| >500 | Any | **Filtered out at Phase 1** | — |
| Unknown | Any | CTO / VP Engineering | CEO / Founder |

**Why HR enters at 200 (earlier than civil's 500)**: tech firms professionalise TA/People ops sooner because tech hiring volume is higher and eng leadership wants to offload the pipeline. Below 200, eng leadership still runs intros; at 200+, there's usually a dedicated TA/HRBP who owns top of funnel.

---

## Phase 2.5: DM Verification (`verify_dms.py`)

Google Search + LinkedIn snippet matching produces false positives — people with a matching title who don't actually work at the target company. Phase 2.5 batches `LinkedIn URL` values through Apify `dev_fusion/Linkedin-Profile-Scraper` and compares scraped `companyName`/`companyWebsite` against the sheet's target.

**Match logic (in priority order):**
1. **Domain root match** — `stripe.com` == `stripe.com` → keep
2. **Squished-name match** — strips punctuation, legal suffixes, lowercases, concatenates. `"Klarna Bank AB"` → `"klarnabank"` matches `"klarna.com"` → keep
3. **Token overlap guard** — if fewer than half of the target's distinct tokens appear in the scraped name, reject

Mismatches clear columns T (DM Name), U (DM Title), V (LinkedIn URL) — leaving K (Company Name) intact so Phase 3 can fall back to `/decision-maker` on that company.

---

## Phase 3: Email Enrichment (`enrich_emails.py`)

**Two modes**, auto-selected per row:

- **Mode A (person):** row has `DM Name` + no `Email` → calls AMF `/find-email/person` with `{full_name, domain}`
- **Mode B (decision-maker):** row has no `DM Name` → calls AMF `/find-email/decision-maker` with `{domain, decision_maker_category}`, primary then fallback

**AMF category mapping (tech, size-based):**

| Company Size | Primary | Fallback |
|---|---|---|
| ≤50 | `ceo` | `engineering` |
| 51–200 | `engineering` | `ceo` |
| 201–500 | `hr` | `engineering` |
| Unknown | `engineering` | `ceo` |

**Valid AMF categories** (exact strings): `ceo, engineering, finance, hr, it, logistics, marketing, operations, buyer, sales`.

**Fallback reporting:** if primary returned an HTTP error and fallback didn't, the fallback result is surfaced in logs (accuracy > optimism). If neither finds a person, writes `"not_found"` so rows aren't retried on every re-run.

**Flags:** `--retry_not_found` reprocesses `email=="not_found"` rows where DM Name is empty (useful after fixing a category config bug). Accepts AMF email statuses `valid` and `risky`.

---

## Phase 4: Email Generation (`generate_emails.py`)

Model: **Claude Opus 4.5** (`claude-opus-4-5`). Template constant uses the same placeholder structure as civil:

```
Noticed {{COMPANY_NAME}} posted a {{ROLE_TITLE}} role. Is this hire a priority in the next 14 days?

Asking because I'm working with a recruiter who has a {{LOCATION}} based {{ROLE_TITLE}} who just became available. {{YEARS}} as a {{ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

Open to interviewing this week if filling this role is urgent.
```

Claude prepends greeting automatically: `Hi {first_name},\n\n{body}`.

**Key rules inside `SHARED_RULES`:**
- **Years floor: NEVER write less than "3+ years"** — if JD says "1+ year" or unstated, default to "3+ years"
- Rewrite role title recruiter-style: specialty leads, level qualifies (`"Senior Python Developer"` → `"Senior Python Engineer"`, `"Sr. Software Engineer II (Platform)"` → `"Senior Platform Engineer"`)
- Strip legal suffixes from company name (`"Klarna Bank AB"` → `"Klarna"`, `"N26 GmbH"` → `"N26"`)
- No em dashes, no exclamation points, commas instead
- ≤75 words
- JSON output: `{"body": "...", "cleaned_role": "..."}`

**Approval gate:** Script refuses to run without `--preview` while `<<TBD>>` is in template. Once approved, `--overwrite` regenerates existing rows.

---

## Phase 5: Instantly Push (`push_campaign.py`)

### Custom variables sent per lead
- `Role` — cleaned_role (falls back to raw job_title if blank)
- `Company name` — cleaned company name
- `Job Link` — Indeed Apply URL
- `Decision Maker Title` — DM title
- `LinkedIn_Url` — DM LinkedIn URL
- `personalization` — full email body

### Sequence (4 steps)
- **Step 1 (day 0):** subject `​{{firstName}}, quick one`, body `{{personalization}}` + iPhone sig
- **Step 2 (+2 days):** subject blank, bump — `just bumping this - still working on the {{Role}} search?`
- **Step 3 (+1 day):** subject blank, check-in + offer profile
- **Step 4 (+1 day):** subject blank, soft break-up

---

## Post-Push Body Sync (Phase 5.5)

If you regenerate email bodies AFTER leads have been pushed (e.g. rule change), the sheet updates but Instantly still holds the old `personalization`. To sync in-place:

1. `POST /api/v2/leads/list` with `{"campaign": "<id>", "limit": 100}` (paginate via `next_starting_after`) to build `email → lead_id` map
2. For each sheet row: `PATCH /api/v2/leads/{lead_id}` with `{"personalization": "<new body>"}`
3. Run 10 workers concurrent — ~30 sec per 100 leads

No standalone script — inline one-shot Python is the current pattern.

---

## Google Sheet Column Layout

Tab: **Leads**. All scripts use this canonical layout (from `pull_dataset.py` HEADERS):

```
A:Job_Id            B:Job Title         C:Job Type          D:Occupations       E:Date Published
F:Salary Min        G:Salary Max        H:Salary Period     I:Apply URL         J:Job Description
K:Company Name      L:Company Website   M:Company Size      N:Revenue           O:CEO Name
P:Company Description  Q:Benefits       R:City              S:State
T:DM Name           U:DM Title          V:LinkedIn URL      W:Email
X:First Name        Y:Last Name         Z:Email Body        AA:Added to Instantly
AB:template_variant AC:cleaned_role
```

---

## Environment

```
APIFY_API_TOKEN=...          # Apify dataset + Google Search + LinkedIn Profile Scraper
ANYMAILFINDER_API_KEY=...    # Email finding (header is "Authorization: {key}", no "Bearer")
ANTHROPIC_API_KEY=...        # Claude Opus 4.5 (emails) + Haiku 4.5 (classification / domains)
INSTANTLY_API_KEY=...        # Campaign creation + lead push
```

Google Sheets OAuth: `.claude/token.json`

---

## Resume Safety

All phases skip already-processed rows:
- Phase 1 (`scrape_and_pull.py`): loads existing Job_Ids from sheet before runs; drops duplicates at ingest
- Phase 1 (`pull_dataset.py` fallback): appends blind — don't re-pull same dataset twice
- Phase 1.75: idempotent — re-run safe
- Phase 1.8: `--apply` deletes dupe rows bottom-up; re-run after apply is a no-op
- Phase 1.9: skips rows where col L already has a valid-looking domain
- Phase 2: skips rows where DM Name is populated
- Phase 2.5: processes all rows with a LinkedIn URL; mismatches clear T/U/V
- Phase 3: skips rows where Email is populated (writes "not_found" to prevent re-query). `--retry_not_found` reprocesses the not-founds
- Phase 4: skips rows where Email Body is populated (`--overwrite` to regenerate)
- Phase 5: skips rows where "Added to Instantly" is TRUE

---

## Why Phase 1.75 (Classify Companies)

Indeed datasets don't distinguish direct employers from agencies/job boards. Cold-emailing tech recruiters (Hays Tech, Oliver James, Robert Walters, Harnham, etc.) wastes sends — they resell labour, they don't hire. Phase 1.75 uses Apify Google Search + Claude Haiku 4.5 to classify each unique company, deletes agency/job_board rows, and populates `Company Website` (col L) for Phase 3 enrichment.
