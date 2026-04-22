---
name: scrape-tech-leads
description: Tech recruitment lead pipeline - scrape tech job postings from TheirStack (Europe + Gulf), find decision makers via AnyMail Finder, enrich emails, generate personalized outreach with Claude (A/B tested), and push to Instantly campaigns. Use when user asks to scrape tech leads, find tech decision makers, enrich emails, generate outreach, or run the tech recruitment pipeline.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Scrape Tech Leads

## Goal
Tech recruitment lead pipeline: scrape tech job postings → find decision makers → enrich emails → generate outreach copy → push to Instantly campaigns.

## Scripts
- `./scripts/scrape_leads.py` — Phase 1: TheirStack Europe → Google Sheets
- `./scripts/scrape_leads_gulf.py` — Phase 1 (Gulf): TheirStack UAE + Saudi Arabia → Google Sheets (no min employee count, default 7 days)
- `./scripts/find_dm.py` — Phase 1.5: Rules-based DM targeting + Apify LinkedIn lookup
- `./scripts/enrich_leads.py` — Phase 2: AnyMail Finder email enrichment + DM fallback
- `./scripts/generate_emails.py` — Phase 3a: Claude-powered outreach copy (2 perm templates + contract, A/B test)
- `./scripts/push_campaign.py` — Phase 3b: Push leads to Instantly campaigns (routes by template_variant)

## Orchestration Flow

When this skill is invoked, follow these steps **in order**, pausing for user confirmation between phases:

### 1. Gather Parameters
Ask the user:
- **Google Sheet URL** (required) — where to store leads
- **Lead limit** (required — MUST ask, never run unlimited)
- **Days posted** (default: 15) — max age of job postings
- Any filter overrides

### 2. Phase 1 — Scrape Jobs
```bash
python3 -W ignore ./scripts/scrape_leads.py --sheet_url "SHEET_URL" --limit 50
```
This will:
- Run a pre-flight count check (limit=1 API call) and show total available
- Wait for user confirmation before scraping
- Scrape in pages of 10 from TheirStack
- Write to the Google Sheet in batches of 10 with 1.5s delay
- Skip duplicates and staffing firms

For Gulf region (UAE + Saudi Arabia):
```bash
python3 -W ignore ./scripts/scrape_leads_gulf.py --sheet_url "SHEET_URL" --limit 50
```
Gulf variant: no minimum employee count (`min_employee_count_or_null: 1`), default 7 days.

### 3. Phase 1.5 — Find Decision Makers
```bash
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL"

# Preview first
python3 -W ignore ./scripts/find_dm.py --sheet_url "SHEET_URL" --dry_run
```

Shows lead count and waits for confirmation before processing.

**DM Rules — Perm:**
- <50 employees → CEO/Founder
- 50-200 → CTO/VP Engineering
- 200-1000 → VP/Head of Engineering
- 1000+ → Director of Engineering / Eng Manager
- Senior hire (Director+, VP, CTO) → always CEO

**DM Rules — Contract:**
- Always CTO/VP Engineering first
- Fallback to CEO/Founder if CTO not found

### 4. Phase 2 — Email Enrichment
```bash
# Both modes: find emails for known DMs + find DMs for rows LinkedIn missed
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL"

# Email only (rows that already have person_name)
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --email_only

# DM fallback only (rows where LinkedIn missed)
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --dm_only --limit 20

# Broad retry: try all DM categories (ceo, engineering, hr) for not-found rows
python3 -W ignore ./scripts/enrich_leads.py --sheet_url "SHEET_URL" --dm_only --broad
```

Two modes:
- **Email-only**: Rows with `person_name` → AMF person endpoint → finds email
- **DM-only**: Rows without `person_name` → AMF decision-maker endpoint → finds DM name + email + title + LinkedIn
- **--broad**: Retries with all three categories (ceo, engineering, hr) for rows where targeted lookup failed

### 5. Phase 3a — Generate Outreach Copy
```bash
# Preview first
python3 -W ignore ./scripts/generate_emails.py --sheet_url "SHEET_URL" --tab "Data" --preview 5

# Generate all
python3 -W ignore ./scripts/generate_emails.py --sheet_url "SHEET_URL" --tab "Data"

# Overwrite existing (regenerate all)
python3 -W ignore ./scripts/generate_emails.py --sheet_url "SHEET_URL" --tab "Data" --overwrite
```

Two perm templates (A/B tested):
- **Perm A** (single candidate, location-specific): "Just became available" framing — location-based candidate, years of experience, industry, two hard-to-fill specialties. Ends with "Open to interviewing this week if filling this role is urgent."
- **Perm B** (recruiter connector): Internal recruiting/inbound framing — connects hiring teams with recruiters placing similar roles. Asks if hire is priority over 15-30 days or exploratory.
- **Contract**: Speed led, immediate availability, handles contracting logistics.

Perm rows get strict alternating A/B assignment (exact 50/50 split). `template_variant` column tracks which template was used. `cleaned_role` column stores the shortened role title.

### 6. Phase 3b — Push to Instantly
```bash
# Push all leads (routes by template_variant automatically)
python3 -W ignore ./scripts/push_campaign.py --sheet_url "SHEET_URL" --tab "Data"

# Preview without pushing
python3 -W ignore ./scripts/push_campaign.py --sheet_url "SHEET_URL" --tab "Data" --dry_run

# Override: push all to a single campaign
python3 -W ignore ./scripts/push_campaign.py --sheet_url "SHEET_URL" --tab "Data" --campaign_id "ID"
```

Routes leads by `template_variant` to campaigns defined in CAMPAIGN_MAP:
- `perm_a` → campaign `26c497f9-44c8-43bd-ba5f-a0ac4e8edaef`
- `perm_b` → campaign `b1ed193b-e892-4f57-942c-0bb185ddf144`

**Fields sent per lead:**
- Standard: `email`, `first_name`, `last_name`
- `personalization` — email body (used as `{{personalization}}` in Instantly step 1)
- Custom variables:
  - `{{Role}}` — cleaned role title
  - `{{Job Link}}` — job posting URL
  - `{{Company name}}` — company name
  - `{{LinkedIn_Url}}` — DM's LinkedIn URL
  - `{{Company_Linkedin}}` — company LinkedIn URL
  - `{{Decision Maker Title}}` — DM's job title

Follow-up sequence (steps 2-4) uses: `{{firstName}}`, `{{Role}}`

Marks "Added to instantly" column TRUE after each batch of 10.

## Environment
```
THEIRSTACK_API_KEY=your_jwt
APIFY_API_TOKEN=your_token
ANYMAILFINDER_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
INSTANTLY_API_KEY=your_key
```

## Google Sheet Structure
Single **Data** tab with columns:

`Job_Id | person_name | result_title | linkedin_url | email | company name | job_title | url | posted_date | job_country_code | is_remote | employment_status | seniority | job_location | job_description | salary | company_url | company_linkedin_url | company_industry | company_employee_count | company_revenue_usd | company_description | company_city | dm_confidence | dm_reasoning | First name | Last name | Body | Added to instantly | template_variant | cleaned_role`

Phase 1 fills: TheirStack data (cols A, F-W)
Phase 1.5 fills: DM data (cols B-D, X-Y)
Phase 2 fills: Email (col E), and DM fallback data (cols B-D) for rows LinkedIn missed
Phase 3a fills: First/Last name (cols Z-AA), Body (col AB), template_variant (col AD), cleaned_role (col AE)
Phase 3b fills: Added to instantly (col AC)