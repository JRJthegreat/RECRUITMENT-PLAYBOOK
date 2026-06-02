---
name: healthcare-linkedin-leads
description: US healthcare clinical recruitment lead pipeline sourced from LinkedIn. Scrapes LinkedIn job postings for Nurse Practitioner roles using insight_api_labs~linkedin-jobs-scraper, filtered to LOW-APPLICANT-COUNT postings (the pain signal), ingests to Google Sheets, enriches company profiles from LinkedIn (website/size/description), then hands off to the 29-col downstream scripts for classify, dedupe, DM-finding, email enrichment, and Instantly push. Targets small NY/MD private practices and clinics hiring clinical staff with no internal TA team.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# healthcare-linkedin-leads

## What This Skill Does

LinkedIn-sourced sibling of `healthcare-leads-indeed`. Instead of scraping Indeed and
filtering by posting age, it scrapes LinkedIn job postings and uses **low applicant count**
(`fewApplicants=True`) as the pain signal — postings few candidates have applied to are where a
practice is most likely to engage outside help.

**Target (demand side):** small private practices and clinics (≤500 employees) hiring Nurse
Practitioners who lack an internal talent-acquisition team. NOT hospital systems, large chains,
FQHCs, government facilities, or staffing agencies posting on behalf of clients.

## Pipeline Phases

| Phase | Script | What it does |
|-------|--------|-------------|
| 1 | `scrape_and_pull.py` | Scrape LinkedIn (low-applicant) → Google Sheet |
| 1.85 | `enrich_company_profiles.py` | Fill website (L), size (M), description (P) from LinkedIn |
| 1.5+ | downstream (see handoff) | classify → dedupe → DM → emails → push |

## Phase 1: Scrape LinkedIn

```bash
python3 -W ignore .claude/skills/healthcare-linkedin-leads/scripts/scrape_and_pull.py \
  --sheet_url "SHEET_URL" \
  --locations "LOCATION" \
  --limit 50 --yes
```

- **Keyword:** defaults to `Nurse Practitioner` (override with `--keywords`).
- **`--locations` is required** — no defaults (to be finalized with Jude). **Semicolon-separated**,
  because LinkedIn city strings contain commas: `--locations "New York, NY;Baltimore, MD"`.
- **No date window.** Pain signal is low applicant count, baked into the actor input as
  `fewApplicants=True`.
- **Company Size (column M) is NOT populated at ingest** — the LinkedIn jobs actor does not return
  headcount. Run `enrich_company_profiles.py` before any DM targeting or size filtering.

## Phase 1.85: Enrich Company Profiles

```bash
# Dry run — lists the LinkedIn company URLs it would enrich
python3 -W ignore .claude/skills/healthcare-linkedin-leads/scripts/enrich_company_profiles.py --sheet_url "SHEET_URL"
# Apply
python3 -W ignore .claude/skills/healthcare-linkedin-leads/scripts/enrich_company_profiles.py --sheet_url "SHEET_URL" --apply
```

Calls `pratikdani~linkedin-company-profile-scraper` per company, writes **L** (real website),
**M** (size, e.g. `51-200 employees`), **P** (description). Resume-safe — skips rows whose col L
is already a non-LinkedIn URL. This replaces the Indeed pipeline's separate `find_company_sizes.py`.

## Downstream Handoff (build when leads + locations are confirmed)

The ingest writes the **29-col schema** shared with `hr-leads-indeed`, so its 29-col scripts read it
correctly: `Company Name=K`, `Company Size=M`, `DM Name=T`, `DM Title=U`, `LinkedIn URL=V`, `Email=W`,
range `A:AA`.

⚠️ **Do NOT reuse `healthcare-leads-indeed/scripts/find_dm.py` or `enrich_emails.py`.** Despite the
docs, those files were repurposed for the **staffing-agency schema** (company=C, DM name=E, title=P,
email=Q, LinkedIn=R, range `A:T`) and will read the wrong columns on a 29-col sheet.

Practices downstream (to build on the 29-col layout, adapting the `hr-leads-indeed` versions):
- **classify_companies.py** — drop hospital systems / chains / FQHCs / govt / staffing agencies; keep
  small private practices.
- **find_dm.py** — 3-pass practice routing → T/U/V: (1) Practice/Office/Clinic Manager,
  (2) Owner / Medical Director / CEO, (3) Managing Partner / Partner. Fixed regardless of size.
- **dedupe_by_company.py / verify_dms.py / find_company_domains.py / enrich_emails.py / push_campaign.py** —
  reuse the `hr-leads-indeed` 29-col versions. `enrich_emails.py` AMF mapping: practice_manager → `hr`
  → `operations` → `ceo`. NP-only ⇒ dedupe role-tiering collapses to an oldest-date tiebreak.

No `generate_emails.py` — Jude writes copy into column Z manually.

## Google Sheet Column Layout (29 cols + AB/AC)

```
A: Job_Id          B: Job Title       C: Job Type        D: Occupations
E: Date Published  F: Salary Min      G: Salary Max      H: Salary Period
I: Apply URL       J: Job Description K: Company Name    L: Company Website
M: Company Size    N: Revenue         O: CEO Name        P: Company Description
Q: Benefits        R: City            S: State           T: DM Name
U: DM Title        V: LinkedIn URL    W: Email           X: First Name
Y: Last Name       Z: Email Body      AA: Added to Inst  AB: Role Type
AC: LinkedIn Job URL
```

**Differences from the Indeed pipeline:**
- Column F (Salary Min): LinkedIn returns a single salary string, not split min/max
- Column M (Company Size): blank at ingest — filled by `enrich_company_profiles.py`
- Column AB: `Role Type` (clinical role; `Nurse Practitioner` for NP-only runs)
- Column AC: `LinkedIn Job URL` (instead of Indeed URL)
- Columns G, H, N, O: blank at ingest (not available from the LinkedIn jobs actor)

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

- **Jobs actor:** `insight_api_labs~linkedin-jobs-scraper` — returns HTTP 201; **does not return
  company size**; `fewApplicants=True` is the low-applicant pain filter; **no `postedAfter`** (no date
  window). LinkedIn `location` string format differs from Indeed — confirm the exact string on the first
  test run for each new city.
- **Company-profile actor:** `pratikdani~linkedin-company-profile-scraper` — sync endpoint, fills
  website / company_size / description.
- **Batch-of-10** sheet writes throughout; resume-safe via existing Job_Ids and the col-V/col-L skip checks.
