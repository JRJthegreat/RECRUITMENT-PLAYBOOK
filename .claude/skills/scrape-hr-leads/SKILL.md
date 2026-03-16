---
name: scrape-hr-leads
description: Full HR lead pipeline - scrape job postings from TheirStack, research decision makers, find emails via AnyMail Finder, generate personalized outreach with Claude, and push to Instantly campaign. Use when user asks to scrape leads, find decision makers, run the lead pipeline, or create an outreach campaign for the HR recruitment client.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Scrape HR Leads

## Goal
End-to-end HR recruitment lead pipeline: job scraping → decision maker research → email finding → personalized outreach → Instantly campaign.

## Scripts
- `./scripts/scrape_leads.py` — Phase 1: TheirStack → Google Sheets (raw jobs)
- `./scripts/enrich_leads.py` — Phase 2: AnyMail Finder → update sheet with emails
- `./scripts/create_campaign.py` — Phase 3: Claude email gen → new Instantly campaign

## Agent
- `decision-maker` — Research agent that determines who to target at each company

## Orchestration Flow

When this skill is invoked, follow these steps **in order**, pausing for user confirmation between phases:

### 1. Gather Parameters
Ask the user:
- **Campaign name** (required) — e.g., "HR Outreach March 2026"
- **Lead count** (default: 10) — how many new leads to scrape
- **Google Sheet URL** (required) — where to store leads. Create a new sheet if user doesn't have one.
- Any filter overrides (days posted, country, employee range)

### 2. Phase 1 — Scrape Jobs
```bash
python3 -u ./scripts/scrape_leads.py --sheet_url "SHEET_URL" --limit 10
```
Show the summary to the user.

### 3. Decision-Maker Research
After Phase 1, read the new rows from the sheet. Batch companies into groups of 5-10 and launch the `decision-maker` agent for each batch.

For each batch, create a temp output file and prompt the agent:
```
Research these companies and determine the right decision maker for each.
Write your results to: /tmp/dm_batch_N.json

Companies:
1. [company_name] ([employee_count] employees, [industry])
   Hiring: [job_title] in [job_location]
   Domain: [company_url]
   Job ID: [job_id]
   Description: [first 200 chars of description]
...
```

After all batches complete:
- Parse the JSON results
- Update the Google Sheet with: target_title, target_person_name, target_linkedin_url, uses_agencies, dm_confidence, dm_reasoning
- **Show a summary table to the user** with company name, DM pick, confidence, and reasoning
- Ask: "Look good? Any adjustments before I find emails?"

### 4. Phase 2 — Find Emails
After user confirms:
```bash
python3 -u ./scripts/enrich_leads.py --sheet_url "SHEET_URL"
```
Show summary: X emails found out of Y leads.

### 5. Phase 3 — Create Campaign
After user confirms:
```bash
python3 -u ./scripts/create_campaign.py --sheet_url "SHEET_URL" --campaign_name "CAMPAIGN_NAME"
```

Use `--dry_run` first if the user wants to preview emails before pushing to Instantly.

## Environment
```
THEIRSTACK_API_KEY=your_jwt
ANYMAILFINDER_API_KEY=your_key
INSTANTLY_API_KEY=your_key
APIFY_API_TOKEN=your_token
ANTHROPIC_API_KEY=your_key
```

## Google Sheet Columns
Phase 1 fills: `company_name, job_title, url, posted_date, country, remote, employment_status, seniority, location, description, salary, company_url, company_linkedin_url, company_industry, company_employee_count, company_revenue, company_description, company_city, job_id`

Agent fills: `target_title, target_person_name, target_linkedin_url, uses_agencies, dm_confidence, dm_reasoning`

Phase 2 fills: `email, email_status`

Phase 3 fills: `message, added_to_campaign`
