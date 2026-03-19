# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

NEXAM AI's HR recruitment lead generation pipeline — a set of Claude Code skills and Python scripts that scrape HR job postings, find decision makers, discover emails, generate personalized outreach, and push leads to Instantly campaigns.

All code lives under `.claude/` (skills, agents, auth, env). There are no top-level source files.

## Pipeline Architecture

The core pipeline is the `scrape-hr-leads` skill with 5 sequential phases, each a standalone Python script:

| Phase | Script | External API | Purpose |
|-------|--------|-------------|---------|
| 1 | `scrape_leads.py` | TheirStack | Scrape HR job postings → Google Sheet |
| 1.5 | `find_dm.py` | Apify LinkedIn Employee Scraper | Rules-based DM targeting + LinkedIn lookup |
| 2 | `enrich_leads.py` | AnyMail Finder (person + DM endpoints) | Find emails for decision makers |
| 3a | `generate_emails.py` | Claude Sonnet API | Generate personalized outreach copy |
| 3b | `push_campaign.py` | Instantly API v2 | Push leads to Instantly campaign |

Scripts are at: `.claude/skills/scrape-hr-leads/scripts/`

Each phase reads from and writes back to a shared Google Sheet. Phases run manually on command, not automatically chained.

## Running Scripts

All scripts use `python3` (macOS has no `python` binary). Suppress warnings with `-W ignore`:

```bash
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/scrape_leads.py --sheet_url "SHEET_URL" --limit 100
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/find_dm.py --sheet_url "SHEET_URL"
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/enrich_leads.py --sheet_url "SHEET_URL" --email_only
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/enrich_leads.py --sheet_url "SHEET_URL" --dm_only
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/generate_emails.py --sheet_url "SHEET_URL"
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/push_campaign.py --sheet_url "SHEET_URL" --campaign_id "ID"
```

## Critical Pattern: Batch-of-10

**All scripts MUST process in batches of 10 and write to the Google Sheet after each batch.** This prevents data loss on crashes and allows resuming by re-running (scripts skip already-processed rows). Never process all leads at once then write in bulk.

## Environment & Auth

- API keys: `.claude/.env` (loaded via `dotenv` relative to script location)
- Required env vars: `THEIRSTACK_API_KEY`, `ANYMAILFINDER_API_KEY`, `INSTANTLY_API_KEY`, `APIFY_API_TOKEN`, `ANTHROPIC_API_KEY`
- Google Sheets OAuth: `.claude/token.json` (setup via `.claude/setup_google_auth.py`)

## API Quirks

- **Apify LinkedIn scraper**: Sync endpoint returns HTTP **201** (not 200). Actor ID: `harvestapi~linkedin-company-employees`. Title is in `currentPositions[0]["title"]`, not `headline`.
- **AnyMail Finder**: Auth header is `Authorization: {API_KEY}` (no "Bearer" prefix). Two endpoints: `/find-email/person` and `/find-email/decision-maker`.
- **Instantly API v2**: Bearer token auth. Leads added one at a time (no bulk endpoint).

## Other Skills

- **casualize-names**: Converts formal names to casual versions (nicknames, stripped company suffixes, city abbreviations) for email personalization. Scripts at `.claude/skills/casualize-names/scripts/`.
- **instantly-autoreply**: Auto-replies to incoming Instantly emails using campaign-specific knowledge bases. Script at `.claude/skills/instantly-autoreply/scripts/`.
- **add-webhook**: Creates Modal webhooks for event-driven execution.
- **local-server**: Runs FastAPI orchestrator locally with Cloudflare tunneling.

## Google Sheet Column Layout (A-AC)

`Job_Id | person_name | result_title | linkedin_url | email | company name | job_title | url | posted_date | job_country_code | is_remote | employment_status | seniority | job_location | job_description | salary | company_url | company_linkedin_url | company_industry | company_employee_count | company_revenue_usd | company_description | company_city | dm_confidence | dm_reasoning | First name | Last name | Body | Added to instantly`
