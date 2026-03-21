# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

NEXAM AI's recruitment lead generation system — Claude Code skills and Python scripts that scrape job postings, find decision makers, discover emails, generate personalized outreach, and push leads to Instantly campaigns. All code lives under `.claude/` (skills, agents, auth, env). There are no top-level source files.

## Two Pipelines

### HR Recruitment (`scrape-hr-leads`)
Targets companies hiring for HR roles. Uses a **decision-maker agent** (Claude-powered web research) to identify the right contact.

### Tech Recruitment (`scrape-tech-leads`)
Targets companies hiring tech talent across Europe. Uses **rules-based DM targeting** + Apify LinkedIn lookup. Splits leads into Perm/Contract tabs with A/B tested email templates.

## Pipeline Phases

Both pipelines follow the same phase structure, each phase a standalone Python script:

| Phase | Script | External API | Purpose |
|-------|--------|-------------|---------|
| 1 | `scrape_leads.py` | TheirStack | Scrape job postings → Google Sheet |
| 1.5 | `find_dm.py` | Apify LinkedIn Scraper | DM targeting + LinkedIn lookup |
| 2 | `enrich_leads.py` | AnyMail Finder | Find emails for decision makers |
| 3a | `generate_emails.py` | Claude API | Generate personalized outreach copy |
| 3b | `push_campaign.py` | Instantly API v2 | Push leads to Instantly campaign |

Scripts live at `.claude/skills/<skill-name>/scripts/`. Each phase reads from and writes back to a shared Google Sheet. Phases run manually, not auto-chained.

**HR pipeline difference:** Phase 1.5 uses the `decision-maker` agent (WebSearch-based research) instead of `find_dm.py`.

## Running Scripts

All scripts use `python3`. Suppress warnings with `-W ignore`:

```bash
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/scrape_leads.py --sheet_url "SHEET_URL" --limit 100
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/find_dm.py --sheet_url "SHEET_URL"
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/enrich_leads.py --sheet_url "SHEET_URL" --email_only
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/enrich_leads.py --sheet_url "SHEET_URL" --dm_only
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/generate_emails.py --sheet_url "SHEET_URL"
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/push_campaign.py --sheet_url "SHEET_URL" --campaign_id "ID"
```

Replace `scrape-hr-leads` with `scrape-tech-leads` for the tech pipeline. Tech pipeline's `find_dm.py` supports `--dry_run` for previewing before updating.

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

## DM Targeting Rules

### Tech Pipeline (rules-based in `find_dm.py`)
**Perm roles by company size:**
- <50 employees: CEO/Founder
- 50-200: CTO/VP Engineering
- 200-1000: VP/Head of Engineering
- 1000+: Director of Engineering
- Senior hire (Director+, VP, CTO): always CEO

**Contract roles:** Always CTO/VP Engineering first, fallback to CEO.

### HR Pipeline (agent-driven)
- <200 employees: CEO/COO/Founder
- 200-1000: VP of People / VP of HR
- 1000+: Director of Talent Acquisition / Head of Recruiting
- Override: If hiring senior HR leader (CHRO, VP HR), always target CEO

## Tech Pipeline Email Templates

Three templates with A/B split for Perm leads:
- **Template A (Pain-Led)**: Multiple candidates framing, proof companies (GBST, Unit4, Casca, ZOPA)
- **Template B (Urgency-Led)**: Single candidate framing, specialties focus
- **Template C (Contract)**: Immediate availability, contracting logistics

Perm rows get random 50/50 A/B split (A or B). Contract rows always get Template C. `template_variant` column tracks assignment.

## Other Skills

- **casualize-names**: Batch converts formal names to casual versions (nicknames, stripped company suffixes, city abbreviations). ~35 records/sec with 5 parallel workers. Scripts at `.claude/skills/casualize-names/scripts/`.
- **instantly-autoreply**: Auto-replies to incoming Instantly emails using campaign-specific knowledge bases (Google Sheet lookup by campaign ID). Script at `.claude/skills/instantly-autoreply/scripts/`.
- **add-webhook**: Creates Modal webhooks for event-driven execution.
- **local-server**: Runs FastAPI orchestrator locally with Cloudflare tunneling.

## Agents

| Agent | Purpose | Model |
|-------|---------|-------|
| `decision-maker` | Research companies and identify DMs with budget authority | Sonnet |
| `code-reviewer` | Unbiased code review (correctness, performance, security) | Sonnet |
| `email-classifier` | Classify Gmail into Action Required / Waiting On / Reference | Sonnet |
| `qa` | Generate tests, run them, report pass/fail | Sonnet |
| `research` | Deep investigation with web + file access | Sonnet |

## Google Sheet Column Layout

`Job_Id | person_name | result_title | linkedin_url | email | company name | job_title | url | posted_date | job_country_code | is_remote | employment_status | seniority | job_location | job_description | salary | company_url | company_linkedin_url | company_industry | company_employee_count | company_revenue_usd | company_description | company_city | dm_confidence | dm_reasoning | First name | Last name | Body | Added to instantly`

Tech pipeline adds `template_variant` column and uses two tabs (Perm, Contract) instead of one.
