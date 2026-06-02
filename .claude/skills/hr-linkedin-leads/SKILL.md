---
name: hr-linkedin-leads
description: US HR specialist recruitment lead pipeline. Scrapes LinkedIn job postings (30-45 days old) for hard-to-fill HR roles using insight_api_labs~linkedin-jobs-scraper, ingests to Google Sheets, classifies out agencies, dedupes by company, finds decision makers, enriches emails, generates personalized outreach, and pushes to Instantly. Targets roles where TA teams fail: Benefits, Total Rewards, Payroll compliance, CHRO/CPO, Labor Relations.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# hr-linkedin-leads

## What This Skill Does

Scrapes LinkedIn for HR specialist job postings that are 30-45 days old — the pain window where companies have failed to fill the role internally and are ready to engage a headhunter. Targets roles where in-house TA consistently fails to source qualified candidates.

**Why 30-45 days:** Companies that just posted (0-14 days) are still optimistic about their own process. Companies at 30-45 days are in pain, have failed, and will sign a contingency agreement without needing to see a candidate first.

**Why these roles:** Benefits compliance (ERISA), Total Rewards, multi-state Payroll, CHRO/CPO (TA can't fill their own boss), and Labor Relations (dying specialty) — all require passive candidate sourcing that generalist TA teams cannot do.

## Pipeline Phases

| Phase | Script | What it does |
|-------|--------|-------------|
| 1 | `scrape_and_pull.py` | Scrape LinkedIn → Google Sheet |
| 1.5 | `hr-leads-indeed/classify_companies.py` | Flag agencies, PEOs, staffing firms |
| 1.6 | `hr-leads-indeed/dedupe_by_company.py` | One row per company |
| 1.7 | `hr-leads-indeed/ai_filter_jobs.py` | Remove non-matching titles |
| 1.8 | `hr-leads-indeed/find_company_sizes.py` | Fill Company Size (M) — required before DM targeting |
| 1.9 | `hr-leads-indeed/find_company_domains.py` | Resolve official company website |
| 2 | `hr-leads-indeed/find_dm.py` | Find decision maker by company size |
| 2.5 | `hr-leads-indeed/verify_dms.py` | Verify DM via LinkedIn profile |
| 3 | `hr-leads-indeed/enrich_emails.py` | Find emails via AnyMail Finder |
| 4 | `hr-leads-indeed/generate_emails.py` | Generate personalized outreach |
| 5 | `hr-leads-indeed/push_campaign.py` | Push to Instantly campaign |

Phases 1.5 onward reuse `hr-leads-indeed` scripts — they operate on the same sheet schema.

## Phase 1: Scrape LinkedIn

```bash
python3 -W ignore .claude/skills/hr-linkedin-leads/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" \
  --min_days 30 --max_days 45 \
  --limit 50 --yes
```

**Important:** Company Size (column M) is NOT populated at ingest — LinkedIn actor does not return headcount. Run `find_company_sizes.py` before DM targeting.

## Phases 1.5–1.9: Classify, Dedupe, Filter, Enrich Company Data

```bash
# Classify out agencies/PEOs
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/classify_companies.py --sheet_url "SHEET_URL"

# Dedupe — one row per company
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/dedupe_by_company.py --sheet_url "SHEET_URL"

# AI filter — remove non-HR / mislabelled titles
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/ai_filter_jobs.py --sheet_url "SHEET_URL"

# Fill company sizes (required before DM targeting)
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_company_sizes.py --sheet_url "SHEET_URL"

# Resolve official company domains
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_company_domains.py --sheet_url "SHEET_URL"
```

## Phase 2: Find Decision Makers

```bash
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_dm.py --sheet_url "SHEET_URL"
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/verify_dms.py --sheet_url "SHEET_URL"
```

## Phase 3: Enrich Emails

```bash
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/enrich_emails.py --sheet_url "SHEET_URL" --email_only
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/enrich_emails.py --sheet_url "SHEET_URL" --dm_only
```

## Phase 4: Generate Emails

**STOP — show email template preview and get approval before running.**

```bash
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/generate_emails.py --sheet_url "SHEET_URL" --preview 3
# After approval:
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/generate_emails.py --sheet_url "SHEET_URL"
```

## Phase 5: Push to Instantly

**STOP — confirm campaign ID before running.**

```bash
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/push_campaign.py --sheet_url "SHEET_URL" --campaign_id "ID"
```

## Default Keywords (12)

```
Benefits Manager         Benefits Director        Benefits Specialist
Total Rewards Manager    Total Rewards Director   Compensation Manager
Payroll Manager          Payroll Director         CHRO
Chief People Officer     Labor Relations Manager  Labor Relations Director
```

## Default Locations (12 US cities)

```
New York, NY    Los Angeles, CA    Chicago, IL     Houston, TX
Atlanta, GA     Dallas, TX         Phoenix, AZ     Seattle, WA
Boston, MA      Miami, FL          Denver, CO      Charlotte, NC
```

Total grid: 12 × 12 = 144 combos per full run.

## DM Targeting Rules (same as hr-leads-indeed)

Applied by `find_dm.py` after `find_company_sizes.py` fills column M:

- <200 employees: CEO / COO / Founder
- 200-1000 employees: VP of People / VP of HR
- 1000+ employees: Director of Talent Acquisition / Head of Recruiting
- If hiring senior HR leader (CHRO, VP HR): always target CEO regardless of size

## Google Sheet Column Layout

```
A: Job_Id          B: Job Title       C: Job Type        D: Occupations
E: Date Published  F: Salary Min      G: Salary Max      H: Salary Period
I: Apply URL       J: Job Description K: Company Name    L: Company Website
M: Company Size    N: Revenue         O: CEO Name        P: Company Description
Q: Benefits        R: City            S: State           T: DM Name
U: DM Title        V: LinkedIn URL    W: Email           X: First Name
Y: Last Name       Z: Email Body      AA: Added to Inst  AB: Seniority
AC: LinkedIn Job URL
```

**Differences from hr-leads-indeed:**
- Column F (Salary Min): LinkedIn returns a single salary string, not split min/max
- Column M (Company Size): blank at ingest — filled by `find_company_sizes.py`
- Column AC: "LinkedIn Job URL" instead of "Indeed URL"
- Columns G, H, N, O, P: blank at ingest (not available from LinkedIn actor)

## Environment

Required in `.claude/.env`:
```
APIFY_API_TOKEN=...
ANYMAILFINDER_API_KEY=...
INSTANTLY_API_KEY=...
ANTHROPIC_API_KEY=...
```

Google Sheets OAuth: `.claude/token.json`

## Actor Notes

- **Actor:** `insight_api_labs~linkedin-jobs-scraper`
- **Response:** HTTP 201
- **Per-run timeout:** 300 seconds (LinkedIn scraping is slow — ~2-3 min per combo)
- **`postedAfter`:** passed as ISO date (45 days ago) to filter at actor level
- **Post-filter:** `publishedAt` checked against `[min_days, max_days]` window at ingest
- **Company size:** NOT returned by actor — must run `find_company_sizes.py` before DM phase
