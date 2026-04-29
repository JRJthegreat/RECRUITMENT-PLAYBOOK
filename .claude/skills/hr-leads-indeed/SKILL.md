---
name: hr-leads-indeed
description: US HR recruitment lead pipeline. Orchestrates Apify Indeed scrapes (valig/indeed-jobs-scraper) across an HR keyword × US-state grid with a 14-day filter, ingests to Google Sheets (≤500 employees), classifies out recruitment agencies and PEOs, dedupes by company, AI-filters non-HR titles, resolves official company domains and LinkedIn-snippet headcounts, finds decision makers via domain-anchored Google Search, enriches emails via AnyMail Finder (person + /decision-maker fallback), generates personalized outreach with Claude, and pushes to Instantly. Use when the user asks to run the HR Indeed lead pipeline, or provides a pre-existing Apify dataset ID for ingestion only.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# HR Leads — Indeed Pipeline

## What This Skill Does

Generates US HR leads from Indeed via Apify. Default flow: the skill runs the Apify `valig/indeed-jobs-scraper` actor across a keyword × US-state grid (last 14 days, ≤500 employees, US), classifies companies to drop recruitment agencies / PEOs / job boards, dedupes by company, AI-filters non-HR titles, resolves official domains and headcounts, finds decision makers via domain-anchored Google Search, enriches emails via AnyMail Finder, generates outreach with Claude, and pushes to an Instantly campaign.

Manual override: `pull_dataset.py` still works for ingesting a dataset ID the user scraped by hand on Apify's web UI.

**Target roles** (typical Indeed scrape filter):
HR Manager, HR Director, HR Generalist, Recruiter, Talent Acquisition, CHRO, Benefits Manager, Benefits Specialist, Payroll Manager.

**Target states**: California (priority), Texas, New York, South Carolina, North Carolina, Nevada, Idaho, Utah, Ohio, Tennessee, Georgia, Missouri.

---

## Pipeline Phases

```bash
# Phase 1 — Scrape Apify Indeed (keyword × US-state grid, 14-day filter, ≤500 employees)
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" [--limit 100] [--days 14] [--workers 8] \
  [--keywords "A,B,..."] [--states "A,B,..."] [--dry_run] [--yes]

# Phase 1 (manual fallback) — Pull a pre-existing Apify dataset ID
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/pull_dataset.py \
  --dataset_id "DATASET_ID" [--sheet_url "URL"] [--sheet_title "Title"] [--limit N]

# Phase 1.75 — Classify companies + delete agencies/PEOs/job boards
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/classify_companies.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.8 — Dedupe by company (keep highest seniority; oldest Date Published wins ties)
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/dedupe_by_company.py \
  --sheet_url "SHEET_URL" [--apply]

# Phase 1.9 — AI filter: drop rows where Job Title isn't genuinely HR/Talent/Benefits/Payroll
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/ai_filter_jobs.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.92 — Find each company's official website domain via Google Search
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_company_domains.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 1.95 — Enrich missing Company Size via LinkedIn company-page snippets
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_company_sizes.py \
  --sheet_url "SHEET_URL" [--apply] [--limit N]

# Phase 2 — Find decision makers via Google Search + LinkedIn
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_dm.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 3 — Enrich emails via AnyMail Finder (single pass: Mode A = /find-email/person
# for rows where Phase 2 found a DM; Mode B = /find-email/decision-maker for rows
# missing a DM but with a domain. Tier-aware AMF category routing via determine_target.)
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/enrich_emails.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 3.5 — Optional AMF rescue with --retry_not_found support; useful to re-attempt
# rows previously written with 'not_found' in col W
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_dm_amf.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run] [--retry_not_found]

# Phase 4 — Generate emails (MUST get user approval on template first)
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/generate_emails.py \
  --sheet_url "SHEET_URL" [--preview N] [--overwrite] [--limit N]

# Phase 5 — Push to Instantly campaign
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_id "ID" --campaign_name "NAME" [--variant A|B]
```

---

## CRITICAL: Email Template Approval

**Phase 4 is NEVER auto-run.** Before running `generate_emails.py`:
1. Show the user the proposed email template(s)
2. Wait for explicit "go ahead" or edits
3. Only then run the script

The user decides which template(s) to use and how to split (A/B, senior/non-senior, etc.).

---

## DM Targeting Rules (Phase 2)

- Senior HR role (VP/Director/Head/C-suite) → CEO
- Unknown company size → CEO
- <200 employees → CEO/Founder
- 200-1000 → VP HR / VP People
- 1000+ → Director TA / Head of Recruiting (filtered out at Phase 1 by ≤500 cap)

DM names found via domain-anchored Google Search:
`("{company}" OR "{domain}") ("{title list}") site:linkedin.com/in/`
Validated by: company-name match + title match + domain-anchor guard for generic names
(rows whose company name lacks any distinctive ≥5-char token are rejected unless the
domain literally appears in the LinkedIn snippet — this stops false positives on
companies like "Institutes of Health"). No location filtering.

---

## Why Phase 1.75 (Classify Companies)

Indeed datasets don't distinguish direct employers from recruitment agencies, staffing firms, PEOs (Insperity, TriNet), RPOs, and job boards. Cold-emailing an agency or PEO wastes sends — they resell HR services, they don't hire HR for themselves. Phase 1.75 uses Apify Google Search + Claude Haiku to classify each unique company, deletes agency / job_board rows, and populates `Company Website` (col L) for Phase 3 enrichment. Cost: ~$0.50 per 200 companies.

---

## Google Sheet Column Layout

Tab: **Leads**

```
Job_Id | Job Title | Job Type | Occupations | Date Published |
Salary Min | Salary Max | Salary Period | Apply URL | Job Description |
Company Name | Company Website | Company Size | Revenue | CEO Name |
Company Description | Benefits | City | State |
DM Name | DM Title | LinkedIn URL | Email |
First Name | Last Name | Email Body | Added to Instantly
```

---

## Environment

```
APIFY_API_TOKEN=...        # Apify dataset fetch + Google Search actor + Indeed scraper
ANYMAILFINDER_API_KEY=...  # AMF email enrichment (Phase 3 + 3.5)
ANTHROPIC_API_KEY=...      # Claude email generation + Haiku classification + AI HR filter
INSTANTLY_API_KEY=...      # Campaign push
```

Google Sheets OAuth: `.claude/token.json`

---

## Resume Safety

All phases skip already-processed rows:
- Phase 1 (`scrape_and_pull.py`): loads existing Job_Ids from sheet before runs; drops duplicates at ingest
- Phase 1 (`pull_dataset.py`, manual fallback): appends blind — don't re-pull same dataset twice
- Phase 1.75: idempotent — re-run safe
- Phase 1.8: `--apply` deletes dupe rows bottom-up; re-run after apply is a no-op
- Phase 1.9: `--apply` deletes non-HR rows; re-run after apply classifies remaining rows again (stable — HR titles keep scoring KEEP)
- Phase 1.92: skips rows where col L already looks like a domain; idempotent
- Phase 1.95: skips rows where col M already has a parseable employee count; idempotent
- Phase 2: skips rows where DM Name is populated; uses primary→fallback tier search; runs cap of 2 Apify queries per row
- Phase 3: skips rows where Email is populated
- Phase 3.5: skips rows where DM Name is populated OR Email is non-empty/non-`not_found`; requires col L domain to fire; writes 'not_found' to col W on miss so re-runs skip (override with `--retry_not_found`)
- Phase 4: skips rows where Email Body is populated (`--overwrite` to regenerate)
- Phase 5: skips rows where "Added to Instantly" is TRUE
