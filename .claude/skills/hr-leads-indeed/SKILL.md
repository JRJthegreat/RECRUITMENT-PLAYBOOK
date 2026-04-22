---
name: hr-leads-indeed
description: HR lead pipeline from Apify Indeed datasets - pull dataset, find decision makers via Google Search, enrich emails via Connector OS, generate personalized outreach with Claude, and push to Instantly. Use when user provides an Apify dataset ID for Indeed HR jobs, or asks to run the indeed lead pipeline.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# HR Leads - Indeed Pipeline

## What This Skill Does

Processes Indeed job postings from Apify datasets. The user scrapes on Apify manually, then provides a dataset ID. This skill handles: pulling data to Google Sheets, finding DMs via Google Search (cheap), enriching emails via Connector OS, generating outreach, and pushing to Instantly.

**Key difference from `scrape-hr-leads`:** No TheirStack scraping. Input is an Apify dataset ID. DM lookup uses Google Search instead of AnyMail Finder. Email enrichment uses Connector OS instead of AnyMail Finder.

---

## Pipeline Phases

```bash
# Phase 1 — Pull Apify dataset into Google Sheet
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/pull_dataset.py \
  --dataset_id "DATASET_ID" [--sheet_title "Title"] [--limit N]

# Phase 2 — Find decision makers via Google Search + LinkedIn
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/find_dm.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 3 — Enrich emails via Connector OS
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/enrich_emails.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

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

Same as `scrape-hr-leads`:
- Senior HR role (VP/Director/Head/C-suite) → CEO
- Unknown company size → CEO
- <200 employees → CEO/Founder
- 200-1000 → VP HR / VP People
- 1000+ → Director TA / Head of Recruiting

DM names found via Google Search: `"{company}" "{target title}" site:linkedin.com/in/`
Validated by: company name match + title match. No location filtering.

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
APIFY_API_TOKEN=...     # Apify dataset fetch + Google Search actor
SSM_API_KEY=...         # Connector OS email finder
ANTHROPIC_API_KEY=...   # Claude email generation
INSTANTLY_API_KEY=...   # Campaign push
```

Google Sheets OAuth: `.claude/token.json`
