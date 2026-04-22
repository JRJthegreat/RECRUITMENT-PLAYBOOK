---
name: civil-engineering-leads-indeed
description: Civil engineering / UK construction lead pipeline. Orchestrates Apify Indeed scrapes (valig/indeed-jobs-scraper) across a keyword × UK-city grid with a 14-day filter, ingests to Google Sheets (≤500 employees), classifies out agencies, dedupes by company, finds decision makers via Google Search (Owner/COO routing), verifies them via LinkedIn profile scrape, enriches emails via AnyMail Finder (person + /decision-maker fallback), generates personalized outreach with Claude Opus 4.5, pushes to Instantly, and patches Instantly leads in-place when bodies are regenerated post-push. Use when the user asks to run the civil engineering Indeed lead pipeline, or provides a pre-existing Apify dataset ID for ingestion only.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Civil Engineering Leads — Indeed Pipeline

## What This Skill Does

Generates UK civil engineering / construction leads from Indeed via Apify. Default flow: the skill runs the Apify `valig/indeed-jobs-scraper` actor across a keyword × city grid (last 14 days, ≤500 employees, UK), classifies companies to drop recruitment agencies, dedupes by company, finds decision makers via Google Search (Owner / COO routing), verifies each DM is actually employed at the target company (LinkedIn profile scrape), enriches emails via AnyMail Finder (person endpoint when DM found, `/decision-maker` endpoint with category fallback when not), generates outreach with Claude Opus 4.5, and pushes to an Instantly campaign.

Manual override: `pull_dataset.py` still works for ingesting a dataset ID the user scraped by hand on Apify's web UI.

**Target roles** (typical Indeed scrape filter):
Civil Engineer, Highways Engineer, Drainage Engineer, Project Manager, Contracts Manager, Structural Engineer, Bridge Engineer, Site Engineer, Sub Agent, Design Engineer, Project Engineer.

**Key differences from `tech-leads-indeed`:**
- No Perm/Contract filter — single template covers both engagement types
- Size cutoff is **>500 employees** — firms above that are typically locked under RPO/PSL contracts
- DM ops layer is **COO / Operations Director / VP Operations** (civil firms don't have a CTO equivalent)
- No HR tier at ≤500 employees — COO/Operations Director runs engineer hiring directly; HR rarely owns the decision at this size

---

## Pipeline Phases

```bash
# Phase 1 — Scrape Apify Indeed (keyword × UK-city grid, 14-day filter, ≤500 employees)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" [--limit 100] [--days 14] [--workers 8] \
  [--keywords "A,B,..."] [--cities "A,B,..."] [--dry_run] [--yes]

# Phase 1 (manual fallback) — Pull a pre-existing Apify dataset ID
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/pull_dataset.py \
  --dataset_id "DATASET_ID" --sheet_url "URL" [--sheet_title "Title"] [--limit N]

# Phase 1.75 — Classify companies + delete agencies/job boards
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/classify_companies.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.8 — Dedupe by company (keep highest seniority; oldest Date Published wins ties)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/dedupe_by_company.py \
  --sheet_url "SHEET_URL" [--apply]

# Phase 2 — Find decision makers via Google Search + LinkedIn (Owner/COO routing)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/find_dm.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 2.5 — Verify DMs actually work at target company (drops false positives)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/verify_dms.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 3 — Enrich emails via AnyMail Finder (two-mode: person + /decision-maker)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/enrich_emails.py \
  --sheet_url "SHEET_URL" [--limit N] [--retry_not_found] [--dry_run]

# Phase 4 — Generate emails (MUST get user approval on TEMPLATE first)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/generate_emails.py \
  --sheet_url "SHEET_URL" [--preview N] [--overwrite] [--limit N]

# Phase 5 — Push to Instantly campaign (new or existing via --campaign_id)
python3 -W ignore .claude/skills/civil-engineering-leads-indeed/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_name "NAME" [--campaign_id "ID"] [--dry_run]

# Phase 5.5 — If regenerating bodies AFTER Phase 5, PATCH Instantly in place
# (see "Post-Push Body Sync" section below — no standalone script yet)
```

---

## DM Targeting Rules (Phase 2)

| Company Size | Job Being Hired | Pass 1 Target | Pass 2 (auto-retry) |
|---|---|---|---|
| <50 | Any | Owner / Managing Director / CEO / Founder | COO / Operations Director |
| 50–200 | Any | COO / Operations Director / VP Operations | Managing Director / CEO |
| 200–500 | C-level role | Managing Director / CEO | COO |
| 200–500 | Any other role | COO / Operations Director | Managing Director / CEO |
| >500 | Any | **Filtered out at Phase 1** | — |
| Unknown | Any | COO / Operations Director | Managing Director / CEO |

**Why no HR tier**: At ≤500 employees, engineer/PM hiring stays with the COO. Cold outreach to HR at this size gets bounced to ops anyway.

---

## Phase 2.5: DM Verification (`verify_dms.py`)

Google Search + LinkedIn snippet matching produces false positives — people with a matching title who don't actually work at the target company. Phase 2.5 batches all `LinkedIn URL` values through Apify `dev_fusion/Linkedin-Profile-Scraper` and compares the scraped `companyName` / `companyWebsite` against the sheet's target company/domain.

**Match logic (in priority order):**
1. **Domain root match** — `zjcgroup.co.uk` == `zjcgroup.co.uk` → keep
2. **Squished-name match** — strips punctuation, legal suffixes, lowercases, concatenates. E.g. `"Yu Group PLC"` → `"yugroupplc"` matches `"yugroupplc.com"` → keep
3. **Token overlap guard** — if fewer than half of the target company's distinct tokens appear in the scraped name, reject. Prevents false "Genus Facilities" vs "Genus Recycling" matches.

Mismatches clear columns **S (DM Name), U (DM Title), V (LinkedIn URL)** — leaving T (Company Name) intact so Phase 3 can fall back to `/decision-maker` on that company.

---

## Phase 3: Email Enrichment (`enrich_emails.py`)

**Two modes**, auto-selected per row:

- **Mode A (person):** row has `DM Name` + no `Email` → calls AMF `/find-email/person` with `{full_name, domain}`
- **Mode B (decision-maker):** row has no `DM Name` → calls AMF `/find-email/decision-maker` with `{domain, decision_maker_category}`, primary then fallback category

**AMF category mapping (size-based):**

| Company Size | Primary | Fallback |
|---|---|---|
| ≤50 | `ceo` | `operations` |
| 51–200 | `operations` | `ceo` |
| 201–500 | `operations` | `hr` |
| 501–1000 | `hr` | `operations` |
| Unknown | `operations` | `ceo` |

**Valid AMF categories** (exact strings): `ceo, engineering, finance, hr, it, logistics, marketing, operations, buyer, sales`. `coo` is **NOT valid** — use `operations`.

**Fallback reporting:** if primary returned an HTTP error and fallback didn't, the fallback result is surfaced in logs (accuracy > optimism). If neither finds a person, writes `"not_found"` so rows aren't retried on every re-run.

**Flags:**
- `--retry_not_found` — reprocesses rows where `email == "not_found"` AND `DM Name` is empty (useful after fixing a category config bug)
- Accepts AMF email statuses `valid` and `risky`

Config: `MAX_WORKERS=20`, `BATCH_SIZE=40`, `SHEET_WRITE_DELAY=0.3`.

---

## Phase 4: Email Generation (`generate_emails.py`)

Model: **Claude Opus 4.5** (`claude-opus-4-5`). Approved template structure (in `TEMPLATE` constant):

```
Noticed {{COMPANY_NAME}} posted a {{ROLE_TITLE}} role. Is this hire a priority in the next 14 days?

Asking because I'm working with a recruiter who has a {{LOCATION}} based {{ROLE_TITLE}} who just became available. {{YEARS}} as a {{ROLE_TITLE}}, {{INDUSTRY}}, strong on {{SPECIALTY_1}} and {{SPECIALTY_2}}.

Open to interviewing this week if filling this role is urgent.
```

Claude prepends greeting automatically: `Hi {first_name},\n\n{body}`.

**Key rules inside `SHARED_RULES`:**
- **Years floor: NEVER write less than "3+ years"** — if JD says "1+ year" or unstated, default to "3+ years"
- Rewrite role title UK-recruiter style: specialty leads, level qualifies (`"Senior Civil Engineer (Highways)"` → `"Senior Highways Engineer"`)
- Strip legal suffixes from company name (`"Balfour Beatty Construction Ltd"` → `"Balfour Beatty"`)
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
- `personalization` — full email body (greeting + Claude-generated paragraphs)

### Instantly campaign sequence (4 steps)

**Step 1 — initial (day 0):**
- Subject: `​{{firstName}}, quick one` (zero-width space prefix)
- Body: `<div>{{personalization}} <br /><br />Jude<br /></div>`

**Step 2 — +2 days (bump):**
- Subject: blank (threads)
- Body: `Hi {{firstName}},` → `Bumping this, is the {{Role}} hire still open?` → `Jude`

**Step 3 — +3 days (check-in + offer):**
- Subject: blank
- Body: `Hi {{firstName}},` → `Quick one, still hiring for the {{Role}} role or has it been parked?` → `Happy to send over the profile if you are still looking.` → `Jude`

**Step 4 — +3 days (break-up):**
- Subject: blank
- Body: `Hi {{firstName}},` → `Going to close this out. If the {{Role}} role opens back up feel free to reach out for the profile.` → `Jude`

If pushing to an existing empty campaign, PATCH the sequence via `/api/v2/campaigns/{id}` with a `sequences` array — see session history for the exact payload shape.

---

## Post-Push Body Sync (Phase 5.5)

If you regenerate email bodies AFTER leads have been pushed (e.g. rule change like "3+ years floor"), the sheet updates but Instantly still holds the old `personalization` snapshot. To sync in-place:

1. `POST /api/v2/leads/list` with `{"campaign": "<id>", "limit": 100}` (paginate via `next_starting_after`) to build `email → lead_id` map
2. For each sheet row: `PATCH /api/v2/leads/{lead_id}` with `{"personalization": "<new body>"}`
3. Run 10 workers concurrent — ~30 sec per 100 leads

No standalone script yet; inline one-shot Python is the current pattern.

---

## Google Sheet Column Layout (LIVE — verified)

Tab: **Leads**. Note: differs from `pull_dataset.py` HEADERS due to historical column reshuffling — this is the authoritative layout all working scripts use.

```
A:Job_Id            B:Job Title         C:Job Type          D:Occupations       E:Date Published
F:Salary Min        G:Salary Max        H:Salary Period     I:Apply URL         J:Job Description
K:Company Website   L:Company Size      M:Revenue           N:CEO Name          O:Company Description
P:Benefits          Q:City              R:State
S:DM Name           T:Company Name      U:DM Title          V:LinkedIn URL      W:Email
X:First Name        Y:Last Name         Z:Email Body        AA:Added to Instantly
AB:template_variant AC:cleaned_role
```

Scripts that read these columns (`generate_emails.py`, `push_campaign.py`, `verify_dms.py`, `enrich_emails.py`) use indexes consistent with this layout. If creating a fresh sheet, align `pull_dataset.py` HEADERS before first use.

---

## Environment

```
APIFY_API_TOKEN=...          # Apify dataset fetch + Google Search + LinkedIn Profile Scraper
ANYMAILFINDER_API_KEY=...    # Email finding (header is "Authorization: {key}", no "Bearer")
ANTHROPIC_API_KEY=...        # Claude Opus 4.5 email generation + Haiku classification
INSTANTLY_API_KEY=...        # Campaign creation + lead push + PATCH
```

Google Sheets OAuth: `.claude/token.json`

---

## Resume Safety

All phases skip already-processed rows:
- Phase 1 (`scrape_and_pull.py`): loads existing Job_Ids from sheet before runs; drops duplicates at ingest
- Phase 1 (`pull_dataset.py`, manual fallback): appends blind — don't re-pull same dataset twice
- Phase 1.75: idempotent — re-run safe
- Phase 1.8: `--apply` deletes dupe rows bottom-up; re-run after apply is a no-op
- Phase 2: skips rows where DM Name is populated
- Phase 2.5: processes all rows with a LinkedIn URL; mismatches clear S/U/V
- Phase 3: skips rows where Email is populated (writes "not_found" to prevent re-query). `--retry_not_found` reprocesses the not-founds for rows without a DM
- Phase 4: skips rows where Email Body is populated (`--overwrite` to regenerate)
- Phase 5: skips rows where "Added to Instantly" is TRUE
- Phase 5.5: idempotent — PATCH is safe to re-run

---

## Why Phase 1.75 (Classify Companies)

Indeed datasets don't distinguish direct employers from agencies/job boards. Cold-emailing an agency wastes sends — they resell labour, they don't hire. Phase 1.75 uses Apify Google Search + Claude Haiku to classify each unique company, deletes agency/job_board rows, and populates `Company Website` (col K) for Phase 3 enrichment. Cost: ~$0.60 per 200 companies.
