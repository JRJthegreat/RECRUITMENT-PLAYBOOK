---
name: tech-leads-indeed
description: Tech lead pipeline from Apify Indeed datasets - pull dataset (Perm-only, ≤500 employees), find decision makers via Google Search, enrich emails via AnyMail Finder, generate personalized outreach with Claude Opus 4.5, and push to Instantly. Use when user provides an Apify dataset ID for Indeed tech jobs, or asks to run the indeed tech lead pipeline.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Tech Leads - Indeed Pipeline

## What This Skill Does

Processes Indeed tech job postings from Apify datasets. The user scrapes on Apify manually, then provides a dataset ID. This skill handles: pulling data to Google Sheets (Perm-only, companies ≤500 employees), finding tech DMs via Google Search, enriching emails via AnyMail Finder, generating outreach with Claude Opus 4.5, and pushing to a fresh Instantly campaign.

**Key differences from `hr-leads-indeed`:**
- Tech-targeted DM rules (CEO/CTO/HR mix, not HR-only)
- AnyMail Finder for email enrichment (not Connector OS)
- Filters out companies >500 employees at ingestion (large enterprises won't engage cold)
- Filters out Contract roles at ingestion (Perm only)
- Claude Opus 4.5 for email generation

---

## Pipeline Phases

```bash
# Phase 1 — Pull Apify dataset into Google Sheet (Perm-only, size ≤500)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/pull_dataset.py \
  --dataset_id "DATASET_ID" [--sheet_title "Title"] [--limit N]

# Phase 2 — Find decision makers via Google Search + LinkedIn
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/find_dm.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 3 — Enrich emails via AnyMail Finder
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/enrich_emails.py \
  --sheet_url "SHEET_URL" [--limit N] [--dry_run]

# Phase 4 — Generate emails (MUST get user approval on template first)
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/generate_emails.py \
  --sheet_url "SHEET_URL" [--preview N] [--overwrite] [--limit N]

# Phase 5 — Push to a NEW Instantly campaign
python3 -W ignore .claude/skills/tech-leads-indeed/scripts/push_campaign.py \
  --sheet_url "SHEET_URL" --campaign_name "NAME" [--campaign_id "ID"] [--dry_run]
```

---

## CRITICAL: Email Template Approval

**Phase 4 is NEVER auto-run.** The `TEMPLATE_PERM` constant at the top of `generate_emails.py` starts as a placeholder. Before running:
1. The user provides the final email copy
2. Slot it into `TEMPLATE_PERM`
3. Show the user the rendered preview (`--preview 3`)
4. Wait for explicit "go ahead"
5. Only then run without `--preview`

---

## DM Targeting Rules (Phase 2)

| Company Size | Job Being Hired | Pass 1 Target | Pass 2 (auto-retry) |
|---|---|---|---|
| <50 | Any | CEO / Founder | CTO |
| 50–200 | Any | CTO | CEO / Founder |
| 200–500 | C-level role (CTO, CIO, CISO, Chief X) | CEO / Founder | CTO |
| 200–500 | Any other role | HR (TA Mgr / HR Director / Head of People) | CTO |
| >500 | Any | **Filtered out at Phase 1** | — |
| Unknown size | Any | CTO | CEO / Founder |

DM names found via Google Search: `"{company}" "{target title}" site:linkedin.com/in/`
Validated by: title keyword match per target category. `is_leadership_title()` filter rejects assistants, interns, contractors, retired profiles, etc.

---

## Google Sheet Column Layout

Tab: **Leads** (single tab — Perm only, no Contract split)

```
A:Job_Id  B:Job Title  C:Job Type  D:Occupations  E:Date Published
F:Salary Min  G:Salary Max  H:Salary Period  I:Apply URL  J:Job Description
K:Company Name  L:Company Website  M:Company Size  N:Revenue  O:CEO Name
P:Company Description  Q:Benefits  R:City  S:State
T:DM Name  U:DM Title  V:LinkedIn URL  W:Email
X:First Name  Y:Last Name  Z:Email Body  AA:Added to Instantly
AB:template_variant  AC:cleaned_role
```

---

## Environment

```
APIFY_API_TOKEN=...          # Apify dataset fetch + Google Search actor
ANYMAILFINDER_API_KEY=...    # Email finding (note: header is "Authorization: {key}", no "Bearer")
ANTHROPIC_API_KEY=...        # Claude Opus 4.5 email generation
INSTANTLY_API_KEY=...        # Campaign creation + lead push
```

Google Sheets OAuth: `.claude/token.json`

---

## Resume Safety

All phases skip already-processed rows:
- Phase 1: appends to existing sheet via `--sheet_url` (no dedup, so don't re-pull the same dataset twice)
- Phase 2: skips rows where DM Name is already populated
- Phase 3: skips rows where Email is already populated (writes "not_found" so we don't retry forever)
- Phase 4: skips rows where Email Body is already populated (use `--overwrite` to regenerate)
- Phase 5: skips rows where "Added to Instantly" is TRUE
