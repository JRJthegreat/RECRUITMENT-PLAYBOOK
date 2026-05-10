---
name: tech-leads-indeed
description: Pan-European tech recruitment lead pipeline. Orchestrates Apify Indeed scrapes (valig/indeed-jobs-scraper) across a keyword × EU-city grid (UK-first) with a 14-day filter, ingests to Google Sheets (≤500 employees), classifies out recruitment agencies, dedupes by company, AI-filters non-engineering titles, resolves official company domains, finds decision makers via Google Search (3-pass CTO → CEO → HR fallback), verifies them via LinkedIn profile scrape, enriches emails via AnyMail Finder (person + /decision-maker fallback), generates personalized outreach with Azure OpenAI GPT-5.1, pushes to Instantly, and patches Instantly leads in-place when bodies are regenerated post-push. Use when the user asks to run the tech Indeed lead pipeline, or provides a pre-existing Apify dataset ID for ingestion only.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Tech Leads — Indeed Pipeline

## What This Skill Does

Generates pan-European tech recruitment leads from Indeed via Apify. Default flow: the skill runs the Apify `valig/indeed-jobs-scraper` actor across a keyword × city grid (last 14 days, ≤500 employees, UK-first then continental EU), classifies companies to drop recruitment agencies/job boards, dedupes by company, AI-filters non-engineering titles via Azure OpenAI GPT-4.1, finds each company's domain, targets decision makers via a 3-pass Google Search (engineering leadership → CEO/Founder → HR/TA safety net), verifies each DM is actually employed at the target company (LinkedIn profile scrape), enriches emails via AnyMail Finder (person endpoint when DM found, `/decision-maker` endpoint with category fallback when not), generates outreach with Azure OpenAI GPT-5.1, and pushes to an Instantly campaign.

Manual override: `pull_dataset.py` still works for ingesting a dataset ID the user scraped by hand on Apify's web UI.

**Target roles** (typical Indeed scrape filter):
Backend Engineer, Frontend Engineer, Full Stack Engineer, DevOps Engineer, Data Engineer, Machine Learning Engineer, Site Reliability Engineer, Mobile Engineer, QA Engineer, Engineering Manager, Head of Engineering, CTO.

**Key differences from `civil-engineering-leads-indeed`:**
- Geography: UK-first then pan-European tech hubs (London/Manchester/Birmingham/Leeds/Edinburgh/Bristol/Cambridge/Oxford/Glasgow/Liverpool/Newcastle/Sheffield, then Dublin/Amsterdam/Berlin/Paris/Munich/Madrid/Barcelona/Lisbon/Stockholm/Copenhagen/Zurich/Warsaw/Milan)
- No Perm/Contract filter (single template covers both)
- DM ops layer is **CTO / VP Engineering / Head of Engineering** (tech firms don't have a COO owning eng hiring)
- HR/TA is a **Pass 3 safety net**, not a primary target — engineering leadership owns hiring at all sizes ≤500

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

# Phase 1.9 — AI relevance filter (Azure OpenAI GPT-4.1 drops non-engineering titles)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/ai_filter_jobs.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.9b — Populate official company domains (required before Phase 2)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/find_company_domains.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 2 — Find decision makers via Google Search + LinkedIn (3-pass CTO → CEO → HR)
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

Engineering leadership owns hiring at all sizes. CEO/Founder is the fallback at smaller sizes and for C-level hires. HR/TA stays as a Pass 3 safety net so we don't lose a lead just because eng leadership is unfindable on Google.

| Company Size | Job Being Hired | Pass 1 Target | Pass 2 Fallback | Pass 3 Fallback |
|---|---|---|---|---|
| <50 | Any | CTO / VP Eng / Head of Eng | CEO / Founder | Head of People / HR Manager / TA |
| 50–200 | Any | CTO / VP Engineering | CEO / Founder | Head of People / HR Manager / TA |
| 200–500 | C-level role (CTO, VP Eng, Engineering Director) | CEO / Founder | COO | — (skipped) |
| 200–500 | Any other eng / IC role | VP / Head of Engineering | CEO / Founder | Head of TA / HR Director |
| >500 | Any | **Filtered out at Phase 1** | — | — |
| Unknown | Any | CTO / VP Engineering | CEO / Founder | Head of People / HR Manager / TA |

**Why no HR primary tier**: TA inboxes at 50–500 are drowned in recruiter pitches and TA at this size is gate-keeping rather than budget-holding. Engineering leadership feels role-vacancy pain directly and responds to candidate-available framing. HR/TA Pass 3 only fires if both engineering leadership and CEO/Founder are unfindable — and is skipped for C-level hires at 200–500 where CEO/COO already cover the buyer.

---

## Phase 1.9: AI Job Relevance Filter (`ai_filter_jobs.py`)

Indeed datasets surface non-engineering roles that slip past keyword scrapes (e.g. "Engineering Manager" returns operations/manufacturing roles, "DevOps" hits sales reps at DevOps tooling vendors). Phase 1.9 reads each row's Job Title + Job Description + Company Name + Company Description through Azure OpenAI GPT-4.1 4.5 and decides whether the role is a placeable engineering / IT / technical IC position.

**KEEP:** Software / Backend / Frontend / Full-Stack / Mobile Engineers (any seniority), DevOps / SRE / Platform / Cloud / Infrastructure, Data / ML / AI / MLOps Engineers, QA / Test Automation / SDET, Embedded / Firmware / Hardware / Robotics, Security / AppSec / DevSecOps Engineers, Software / Solution / Cloud / Data Architects, Engineering Managers, Tech Leads, Heads / VPs / Directors of Engineering, CTOs, Forward-Deployed / Solutions / Sales Engineers (technical post-sale).

**DROP:** Sales / Account Exec / BDR, Marketing / Brand / Growth, HR / People / Talent / Recruiter, Customer Success / Support, non-technical Project / Programme / Delivery Managers, Product Managers (we place engineers, not PMs), Designers (UX/UI/Graphic/Brand), Business Analysts / PMO Analysts, Finance / Legal / Operations Managers, IT Support / Help Desk, Researchers / Academics, drivers / warehouse / labourers, healthcare / teachers / estate agents.

Borderline → KEEP (err inclusive on engineering-adjacent titles).

Dry-run by default; `--apply` deletes DROP rows.

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

**AMF category mapping (tech):**

| Company Size | Primary | Fallback |
|---|---|---|
| ≤50 | `engineering` | `hr` |
| 51–200 | `engineering` | `hr` |
| 201–500 | `engineering` | `hr` |
| Unknown | `engineering` | `hr` |

`engineering` is the primary at all sizes (matches the Phase 2 Pass 1 model). `hr` covers TA/People/HR roles at AMF and is the safety-net fallback.

**Valid AMF categories** (exact strings): `ceo, engineering, finance, hr, it, logistics, marketing, operations, buyer, sales`.

**Fallback reporting:** if primary returned an HTTP error and fallback didn't, the fallback result is surfaced in logs (accuracy > optimism). If neither finds a person, writes `"not_found"` so rows aren't retried on every re-run.

**Flags:** `--retry_not_found` reprocesses `email=="not_found"` rows where DM Name is empty (useful after fixing a category config bug). Accepts AMF email statuses `valid` and `risky`.

---

## Phase 4: Email Generation (`generate_emails.py`)

Model: **Azure OpenAI GPT-5.1** (deployment from `AZURE_OPENAI_DEPLOYMENT`). Template constant uses the same placeholder structure as civil:

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
APIFY_API_TOKEN=...                # Apify dataset + Google Search + LinkedIn Profile Scraper
ANYMAILFINDER_API_KEY=...          # Email finding (header is "Authorization: {key}", no "Bearer")
AZURE_OPENAI_ENDPOINT=...          # Azure OpenAI endpoint URL
AZURE_OPENAI_API_KEY=...           # Azure OpenAI API key
AZURE_OPENAI_API_VERSION=...       # default "2024-10-21"
AZURE_OPENAI_DEPLOYMENT=...        # GPT-5.1 deployment name (Phase 4 email generation)
AZURE_OPENAI_DEPLOYMENT_FAST=...   # GPT-4.1 deployment name (Phase 1.75/1.9/1.9b classification + filtering)
INSTANTLY_API_KEY=...              # Campaign creation + lead push
```

Google Sheets OAuth: `.claude/token.json`

---

## Resume Safety

All phases skip already-processed rows:
- Phase 1 (`scrape_and_pull.py`): loads existing Job_Ids from sheet before runs; drops duplicates at ingest
- Phase 1 (`pull_dataset.py` fallback): appends blind — don't re-pull same dataset twice
- Phase 1.75: idempotent — re-run safe
- Phase 1.8: `--apply` deletes dupe rows bottom-up; re-run after apply is a no-op
- Phase 1.9 (`ai_filter_jobs.py`): dry-run by default; `--apply` deletes DROP rows bottom-up
- Phase 1.9b (`find_company_domains.py`): skips rows where col L already has a valid-looking domain
- Phase 2: skips rows where DM Name is populated. Pass 2 / Pass 3 only run against rows still missing a DM after the previous pass
- Phase 2.5: processes all rows with a LinkedIn URL; mismatches clear T/U/V
- Phase 3: skips rows where Email is populated (writes "not_found" to prevent re-query). `--retry_not_found` reprocesses the not-founds
- Phase 4: skips rows where Email Body is populated (`--overwrite` to regenerate)
- Phase 5: skips rows where "Added to Instantly" is TRUE

---

## Why Phase 1.75 (Classify Companies)

Indeed datasets don't distinguish direct employers from agencies/job boards. Cold-emailing tech recruiters (Hays Tech, Oliver James, Robert Walters, Harnham, etc.) wastes sends — they resell labour, they don't hire. Phase 1.75 uses Apify Google Search + Azure OpenAI GPT-4.1 4.5 to classify each unique company, deletes agency/job_board rows, and populates `Company Website` (col L) for Phase 3 enrichment.
