---
name: scrape-hr-leads
description: Full HR lead pipeline - scrape job postings from TheirStack, research decision makers, find emails via AnyMail Finder, generate personalized outreach with Claude, and push to Instantly campaign. Use when user asks to scrape leads, find decision makers, run the lead pipeline, or create an outreach campaign for the HR recruitment client.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Scrape HR Leads

## What This Skill Does

Scrapes HR job postings and finds decision maker emails. **Stops there** — email copy and campaign push are handled manually.

**Skill output:** Google Sheet URL with leads + DM emails populated, ready for copy generation.

**The only thing that changes between campaigns:** `--days` (how far back to scrape).

---

## Run Commands (Skill Scope)

```bash
# Step 1 — Scrape (shows total count first, asks to confirm before burning credits)
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/scrape_leads.py \
  --sheet_url "SHEET_URL" --days 30

# Step 2 — Find DMs + emails (Pass 1 + auto-retry in one run)
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/find_dm.py \
  --sheet_url "SHEET_URL"
```

After Step 2 completes, output the Google Sheet URL to the user. Done.

---

## Manual Steps (handled by user, not this skill)

```bash
# Generate emails (run manually when ready)
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/generate_emails.py \
  --sheet_url "SHEET_URL"

# Push to Instantly campaign (run manually after reviewing emails)
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_name "HR Leads DD MM"

# Or push to existing campaign:
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_id "CAMPAIGN_ID" --campaign_name "HR Leads DD MM"
```

---

## Scripts

| Script | Phase | What it does |
|--------|-------|-------------|
| `scrape_leads.py` | 1 | TheirStack → Google Sheets. Deduplicates, filters staffing firms, one job per company. |
| `find_dm.py` | 1.5 | AnyMail Finder DM lookup → person_name, email, title, linkedin. Two passes: primary + auto-retry with flipped category. |
| `generate_emails.py` | 3a | Claude Opus 4 email generation → First name, Last name, Body, cleaned_role. |
| `push_campaign.py` | 3b | Creates Instantly campaign (or uses existing), pushes leads with all custom variables, retries failures. |
| `enrich_leads.py` | 2 | Fallback email finder — only needed if find_dm.py misses rows. |

---

## Key Flags

### scrape_leads.py
- `--days N` — scrape jobs posted in last N days (default: 15)
- `--limit N` — cap results (skips confirmation prompt)
- `--yes` — skip confirmation prompt for automation
- `--start_page N` — resume from page N after a crash

### find_dm.py
- `--limit N` — process only first N leads in Pass 1
- `--dry_run` — preview DM targeting rules without calling AnyMail
- `--no_retry` — skip Pass 2 retry (debugging only)

### generate_emails.py
- `--preview N` — print first N emails without writing to sheet
- `--overwrite` — regenerate emails that already have a Body
- `--limit N` — cap generation

### push_campaign.py
- `--campaign_name` — name for the new campaign (required)
- `--campaign_id` — use existing campaign instead of creating one
- `--dry_run` — preview leads without pushing

---

## Rate Limit Rules (strictly enforced in all scripts)

| API | Limit | How we handle it |
|-----|-------|-----------------|
| Google Sheets | 60 writes/min | Batches of 10, 1.5s sleep between each batch |
| TheirStack | credits per result | Preview call first (shows total), confirmation required if >500 and no --limit |
| AnyMail Finder | credits per call | 5 parallel workers, 180s timeout |
| Claude API | token rate limit | Exponential backoff (2^retry × 2s), max 3 retries |
| Instantly | 30s timeout | Sequential push, 3 retries with 5s delay for failures |

**Sheet write failures in scrape_leads.py are fatal** — the script stops and prints the exact `--start_page N` to resume from. This prevents burning TheirStack credits on rows that can't be saved.

---

## DM Targeting Rules (find_dm.py)

Primary category selection:
- Hiring **senior HR leader** (VP/Director/Head/C-suite) → `["ceo"]`
- Company size **unknown** → `["ceo"]`
- **< 200 employees** → `["ceo"]`
- **200–1000 employees** → `["hr"]`
- **1000+ employees** → `["hr"]`

Auto-retry (Pass 2) flips the category: `["ceo"]` → `["hr"]`, `["hr"]` → `["ceo"]`

---

## Google Sheet Column Layout

Tab: **Data**

```
Job_Id | person_name | result_title | linkedin_url | email |
company name | job_title | url | posted_date | job_country_code |
is_remote | employment_status | seniority | job_location | job_description |
salary | company_url | company_linkedin_url | company_industry |
company_employee_count | company_revenue_usd | company_description | company_city |
dm_confidence | dm_reasoning | First name | Last name | Body | Added to instantly |
cleaned_role
```

---

## Instantly Campaign Structure (hardcoded in push_campaign.py)

- Schedule: Mon–Fri, 09:00–18:00, America/Detroit timezone
- 4-step follow-up sequence (delays: +2d, +1d, +1d, +1d)
- Subject step 1: `{{firstName}}, quick one`
- Body: `{{personalization}}` (= full email body from the sheet)
- Custom variables populated: `Role`, `Job Link`, `Company name`, `LinkedIn_Url`, `Company_Linkedin`, `Decision Maker Title`
- Settings: text-only, stop on reply, no open/link tracking, daily limit 2500

---

## Environment

```
THEIRSTACK_API_KEY=...
ANYMAILFINDER_API_KEY=...
ANTHROPIC_API_KEY=...
INSTANTLY_API_KEY=...
```

Google Sheets OAuth token: `.claude/token.json` (set up via `.claude/setup_google_auth.py`)
