---
name: scrape-tech-leads
description: Tech recruitment lead pipeline - scrape tech job postings from TheirStack (Europe), classify perm vs contract, research decision makers via Apify LinkedIn scraper, find emails via AnyMail Finder, generate personalized outreach with Claude, and write to Google Sheets. Use when user asks to scrape tech leads, find tech decision makers, enrich emails, generate outreach, or run the tech recruitment pipeline.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Scrape Tech Leads

## Goal
Tech recruitment lead pipeline: scrape European tech job postings → classify perm/contract → find decision makers → enrich emails → generate outreach copy.

## Scripts
- `./scripts/scrape_leads.py` — Phase 1: TheirStack → Google Sheets (Perm + Contract tabs)
- `./scripts/find_dm.py` — Phase 1.5: Rules-based DM targeting + Apify LinkedIn lookup
- `./scripts/enrich_leads.py` — Phase 2: AnyMail Finder email enrichment + DM fallback
- `./scripts/generate_emails.py` — Phase 3a: Claude-powered outreach copy (3 templates, A/B test)

## Orchestration Flow

When this skill is invoked, follow these steps **in order**, pausing for user confirmation between phases:

### 1. Gather Parameters
Ask the user:
- **Google Sheet URL** (required) — where to store leads
- **Lead limit** (default: all) — how many new leads to scrape
- **Days posted** (default: 15) — max age of job postings
- Any filter overrides

### 2. Phase 1 — Scrape Jobs
```bash
python3 -W ignore ./scripts/scrape_leads.py --sheet_url "SHEET_URL" --limit 50
```
This will:
- Call TheirStack for European tech roles
- Classify each job as perm or contract
- Write to the **Perm** or **Contract** tab accordingly
- Skip duplicates and staffing firms

Show the summary (perm count, contract count, duplicates skipped).

### 3. Phase 1.5 — Find Decision Makers
After Phase 1, ask which tab(s) to process:
```bash
# Both tabs (default)
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL" --tab both

# Or just one tab
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL" --tab perm
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL" --tab contract
```

Use `--dry_run` first to preview which DM each company would target:
```bash
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL" --dry_run
```

**DM Rules — Perm:**
- <50 employees → CEO/Founder
- 50-200 → CTO/VP Engineering
- 200-1000 → VP/Head of Engineering
- 1000+ → Director of Engineering / Eng Manager
- Senior hire (Director+, VP, CTO) → always CEO

**DM Rules — Contract:**
- Always CTO/VP Engineering first (they feel the contract hiring pain)
- Fallback to CEO/Founder if CTO not found

Show summary: found count, not found count, confidence breakdown.

### 4. Phase 2 — Email Enrichment
After Phase 1.5, enrich leads with emails via AnyMail Finder:
```bash
# Both modes: find emails for known DMs + find DMs for rows LinkedIn missed
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --tab "Perm (High-Pay),Contract (High-Pay)"

# Email only (rows that already have person_name)
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --email_only

# DM fallback only (rows where LinkedIn missed)
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --dm_only --limit 20
```

Two modes:
- **Email-only**: Rows with `person_name` → AMF person endpoint → finds email
- **DM-only**: Rows without `person_name` → AMF decision-maker endpoint → finds DM name + email + title + LinkedIn

DM categories for tech: Contract → always CTO. Perm senior hire → CEO. Perm small/unknown → CEO. Perm 50+ → CTO.

Show summary: emails found, DMs found, not found.

### 5. Phase 3a — Generate Outreach Copy
After Phase 2, generate personalized emails:
```bash
# Preview first
python3 -W ignore ./scripts/generate_emails.py --sheet_url "SHEET_URL" --tab "Perm (High-Pay),Contract (High-Pay)" --preview 5

# Generate all
python3 -W ignore ./scripts/generate_emails.py --sheet_url "SHEET_URL" --tab "Perm (High-Pay),Contract (High-Pay)"
```

Three templates (CTAs added separately in Instantly):
- **Perm A** (pain point led): Multiple candidates framing, uses proof companies (GBST, Unit4, Casca, ZOPA)
- **Perm B** (urgency led): Single candidate framing, uses specialties
- **Contract**: Speed led, immediate availability, handles contracting logistics

Perm rows get randomly assigned A or B (50/50 A/B test). `template_variant` column tracks which template was used.

Show summary: generated count, A/B/contract breakdown, failed count.

## Environment
```
THEIRSTACK_API_KEY=your_jwt
APIFY_API_TOKEN=your_token
ANYMAILFINDER_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
```

## Google Sheet Structure
Two tabs: **Perm** and **Contract**, each with the same column layout (A-AC):

`Job_Id | person_name | result_title | linkedin_url | email | company name | job_title | url | posted_date | job_country_code | is_remote | employment_status | seniority | job_location | job_description | salary | company_url | company_linkedin_url | company_industry | company_employee_count | company_revenue_usd | company_description | company_city | dm_confidence | dm_reasoning | First name | Last name | Body | Added to instantly | template_variant`

Phase 1 fills: TheirStack data (cols A, F-W)
Phase 1.5 fills: DM data (cols B-D, X-Y)
Phase 2 fills: Email (col E), and DM fallback data (cols B-D) for rows LinkedIn missed
Phase 3a fills: First/Last name (cols Z-AA), Body (col AB), template_variant (col AD)
