# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

NEXAM AI's recruitment lead generation system — Claude Code skills and Python scripts that scrape job postings, find decision makers, discover emails, generate personalized outreach, and push leads to Instantly campaigns. All code lives under `.claude/` (skills, agents, auth, env). There are no top-level source files.

## All Pipelines

Each pipeline is a Claude Code skill with its own `SKILL.md` (authoritative detail) and `scripts/` directory.

| Skill | Source | Niche | Geography |
|-------|--------|-------|-----------|
| `scrape-hr-leads` | TheirStack | HR roles | US |
| `scrape-tech-leads` | TheirStack | Tech roles | Europe + Gulf |
| `hr-leads-indeed` | Indeed (Apify) | HR roles | US states |
| `hr-linkedin-leads` | LinkedIn (Apify) | HR specialist roles (30-45 day pain window) | US cities |
| `tech-leads-indeed` | Indeed (Apify) | Engineering roles | Pan-EU cities |
| `civil-engineering-leads-indeed` | Indeed (Apify) | Civil/construction roles | UK cities |
| `healthcare-leads-indeed` | Indeed (Apify) | Family Medicine/NP/PA | NY + MD |
| `healthcare-linkedin-leads` | LinkedIn (Apify) | Nurse Practitioner (low-applicant signal) | NY + MD |
| `verify-leads` | Any sheet | Re-verify DMs + emails | Any |
| `sba-campaigns` | SBA data | Borrower + lender outreach | US |
| `healthcare-staffing-enrichment` | Any sheet | Classify/enrich healthcare staffing agencies | US |

Utilities: `casualize-names`, `instantly-autoreply`, `add-webhook`, `local-server`.

## Phase Architecture (Indeed / LinkedIn pipelines)

The Indeed and LinkedIn pipelines share a common phase skeleton. All detailed phase commands live in each skill's `SKILL.md` — treat that as the authoritative reference.

| Phase | Script | Purpose |
|-------|--------|---------|
| 1 | `scrape_and_pull.py` | Scrape source → new Google Sheet |
| 1 (fallback) | `pull_dataset.py` | Ingest a pre-existing Apify dataset ID |
| 1.75 | `classify_companies.py` | LLM-classify companies; delete agencies / job boards |
| 1.8 | `dedupe_by_company.py` | One row per company (highest seniority wins; oldest posting tiebreaks) |
| 1.9 | `ai_filter_jobs.py` | AI relevance filter — drops non-target job titles |
| 1.9x | `find_company_domains.py` / `find_company_sizes.py` | Resolve official domain + headcount |
| 2 | `find_dm.py` | Find decision maker via Google Search + LinkedIn snippets |
| 2.5 | `verify_dms.py` | Verify DM is actually employed at target (Apify LinkedIn profile scrape) |
| 3 | `enrich_emails.py` | AnyMail Finder — person endpoint (DM known) or /decision-maker fallback |
| 3.5 | `find_dm_amf.py` | AMF rescue pass — retry `not_found` rows; `hr-leads-indeed` only |
| 4 | `generate_emails.py` | LLM-generate personalized email body (**requires template approval first**) |
| 5 | `push_campaign.py` | Push to Instantly campaign one lead at a time |

Not every pipeline has every phase — check the skill's `SKILL.md` for the exact sequence.

**`healthcare-leads-indeed` has extra manual steps** between Phase 1 and Phase 1.75 (sort by Date Published, delete pre-2026 rows, keyword-filter company names, remove >500-employee rows) that have no scripts — they're inline operations described in that SKILL.md.

**TheirStack pipelines (`scrape-hr-leads`, `scrape-tech-leads`)** use a simpler 5-phase structure: `scrape_leads.py` → `find_dm.py` → `enrich_leads.py` → `generate_emails.py` → `push_campaign.py`.

## Running Scripts

All scripts use `python3 -W ignore`. Pass `--sheet_url` as the first argument. Each skill's `SKILL.md` has the exact command with all flags.

```bash
# Example — Indeed pipeline phase 1
python3 -W ignore .claude/skills/hr-leads-indeed/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" --limit 100 --days 14

# Example — TheirStack pipeline
python3 -W ignore .claude/skills/scrape-hr-leads/scripts/scrape_leads.py \
  --sheet_url "SHEET_URL" --limit 100
```

Scripts are run from the repo root. The `.env` is loaded relative to script location.

## Critical Rules

**Batch-of-10:** Every script MUST write to the Google Sheet after each batch of 10 rows — never batch all then write. This enables crash recovery and idempotent reruns (scripts skip already-processed rows).

**Email template approval gate:** Phase 4 (`generate_emails.py`) MUST NOT run until the user has seen and approved the template. Show `--preview N` output first, wait for explicit approval, then run for real.

**Phase 4 and Phase 5 are manual stops** — never auto-chain into them.

**No sending accounts:** Never add sending accounts to Instantly campaigns; Jude configures those manually in the UI.

**Never delete rows without asking** — always confirm before calling `--apply` on destructive steps.

## DM Targeting Rules

Rules are pipeline-specific; see each SKILL.md. Summary:

| Pipeline | DM Target Logic |
|----------|----------------|
| `hr-leads-indeed` / `hr-linkedin-leads` | Size-based: CEO (<50), HR Manager (50-200), VP HR (200-500). Senior role (CHRO/VP HR) → always CEO |
| `tech-leads-indeed` | 3-pass: CTO/VP Eng → CEO/Founder → Head of People (safety net) |
| `civil-engineering-leads-indeed` | 2-pass: Owner/MD/CEO (<50) or COO/Ops Director (50-200) → fallback |
| `healthcare-leads-indeed` / `healthcare-linkedin-leads` | 3-pass fixed: Practice/Office Manager → Owner/Medical Director/CEO → Managing Partner |

## LLM Provider

| Pipeline | Classification / Filtering | Email Generation |
|----------|---------------------------|-----------------|
| `tech-leads-indeed` | Azure OpenAI GPT-4.1 (`AZURE_OPENAI_DEPLOYMENT_FAST`) | Azure OpenAI GPT-5.1 (`AZURE_OPENAI_DEPLOYMENT`) |
| `civil-engineering-leads-indeed` | Claude Haiku | Claude Opus 4.5 |
| All others | Claude Haiku (via `ANTHROPIC_API_KEY`) | Claude (model per skill) |

Azure OpenAI env vars: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` (default `2024-10-21`), `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_DEPLOYMENT_FAST`.

## Environment & Auth

- API keys: `.claude/.env` (loaded via `dotenv` relative to script location)
- Core env vars: `APIFY_API_TOKEN`, `ANYMAILFINDER_API_KEY`, `INSTANTLY_API_KEY`, `ANTHROPIC_API_KEY`
- TheirStack pipelines also need: `THEIRSTACK_API_KEY`
- Google Sheets OAuth: `.claude/token.json` (setup via `.claude/setup_google_auth.py`)

## API Quirks

- **Apify LinkedIn scraper (employees):** Sync endpoint returns HTTP **201**. Actor: `harvestapi~linkedin-company-employees`. Title is in `currentPositions[0]["title"]`.
- **Apify LinkedIn jobs scraper:** `insight_api_labs~linkedin-jobs-scraper` — HTTP 201; no company size in response; `fewApplicants=True` for low-applicant pain filter.
- **Apify LinkedIn profile scraper:** `dev_fusion/Linkedin-Profile-Scraper` — used in Phase 2.5 DM verification.
- **Apify LinkedIn company profile:** `pratikdani~linkedin-company-profile-scraper` — fills website/size/description for LinkedIn-sourced pipelines.
- **AnyMail Finder:** Auth header is `Authorization: {API_KEY}` (no "Bearer"). Two endpoints: `/find-email/person` and `/find-email/decision-maker`. Valid categories: `ceo, engineering, finance, hr, it, logistics, marketing, operations, buyer, sales` (`coo` is NOT valid — use `operations`).
- **Instantly API v2:** Bearer token auth. Leads added one at a time (no bulk endpoint). `DELETE /leads/{id}` is safe; `DELETE /leads` with a body wipes the **entire campaign**.
- **Google Search actor:** Used by `classify_companies.py`, `find_company_domains.py`, `find_dm.py` — runs via Apify, not direct Google API.

## DM Verification (Phase 2.5)

`verify_dms.py` scrapes each LinkedIn URL via Apify and compares `companyName`/`companyWebsite` against the sheet's target. Match priority:
1. Domain root match
2. Squished-name match (strips punctuation/legal suffixes, concatenates)
3. Token overlap guard (>50% of target's tokens must appear in scraped name)

Mismatches clear DM Name / DM Title / LinkedIn URL columns — leaving the company domain intact so Phase 3 can fall back to `/decision-maker`.

## Google Sheet Column Schemas

All Indeed/LinkedIn pipelines use a **29-column base schema** with pipeline-specific differences in the final columns. Canonical layout:

```
A:Job_Id     B:Job Title    C:Job Type       D:Occupations     E:Date Published
F:Salary Min G:Salary Max   H:Salary Period  I:Apply URL       J:Job Description
K:Company Name  L:Company Website  M:Company Size  N:Revenue  O:CEO Name
P:Company Description  Q:Benefits  R:City  S:State
T:DM Name  U:DM Title  V:LinkedIn URL  W:Email
X:First Name  Y:Last Name  Z:Email Body  AA:Added to Instantly
AB:pipeline-specific  AC:pipeline-specific
```

**healthcare-leads-indeed** differs (no Company Name col; historical reshuffling — see that SKILL.md for the authoritative layout). **tech-leads-indeed** and **civil-engineering-leads-indeed** also have minor column differences — each SKILL.md has the verified layout.

**`tech-leads-indeed`** adds `AB:template_variant` and `AC:cleaned_role` (populated by `generate_emails.py`).

⚠️ Do not copy scripts between healthcare-leads-indeed and other pipelines — its `find_dm.py` and `enrich_emails.py` were repurposed for a staffing-agency schema with different column positions.

## Shared Utility Scripts

`.claude/scripts/` contains one-off and cross-pipeline utilities (not part of any skill's standard pipeline):
- `ingest_apify.py` — generic Apify dataset ingestion
- `research_dm.py` — standalone DM research
- `generate_emails_uk.py` / `generate_emails_v2.py` — legacy/experimental email generators
- `verify_emails.py` / `verify_emails_uk.py` — email verification passes
- `salesnav_enrich.py` / `salesnav_about_enrich.py` — Sales Navigator enrichment
- `consolidate_tabs.py`, `filter_icp.py`, `reenrich_invalids.py` — sheet maintenance utilities
- `push_campaign_uk.py`, `wipe_campaign_uk.py` — UK campaign variants (use with caution — `wipe_campaign_uk.py` is destructive)
- `scrape_linkedin_profiles.py` — standalone LinkedIn profile batch scrape
- `_inspect_*.py`, `_stats.py`, `_reorder_columns.py`, `_remove_unclassified.py` — diagnostics (prefixed `_` = dev tools, not pipeline steps)

## SBA Campaigns

`sba-campaigns` has no `SKILL.md`. It is a two-track outreach system using SBA loan data:

- **Borrower track:** `find_borrower_websites.py` → `find_borrower_dms.py` → `enrich_borrower_emails.py` → `render_bodies.py` → `push_campaigns.py`
- **Lender track:** `find_lender_dms.py` → `patch_lender_leads.py` → `push_campaigns.py`

Lead lists come from `borrowers.txt` / `lenders.txt` in the skill directory. No Apify scrape step.

## verify-leads Quirks

`verify-leads` takes **0-based column indices** as CLI args (A=0, B=1, …) instead of letter names. The scripts are schema-agnostic — you pass `--col_name`, `--col_website`, `--col_dm_name`, etc. for each run. Always check the exact flags against `verify-leads/SKILL.md` before running.

## healthcare-staffing-enrichment

Standalone enrichment skill for healthcare staffing agency sheets (the supply side of the healthcare pipeline). No SKILL.md. Phase order: `classify_agencies.py` (drops non-agencies via GPT-4.1) → `find_websites.py` / `find_missing_websites.py` → `enrich_linkedin_company.py` → `enrich_company_profiles.py` → `find_ceo.py`. Uses Azure OpenAI for classification.

## Agents

| Agent | Purpose | Model |
|-------|---------|-------|
| `decision-maker` | Research companies and identify DMs with budget authority | Sonnet |
| `code-reviewer` | Unbiased code review (correctness, performance, security) | Sonnet |
| `email-classifier` | Classify Gmail into Action Required / Waiting On / Reference | Sonnet |
| `qa` | Generate tests, run them, report pass/fail | Sonnet |
| `research` | Deep investigation with web + file access | Sonnet |
