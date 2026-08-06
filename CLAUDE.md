# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

NEXAM AI's recruitment lead generation system — Claude Code skills and Python scripts that scrape job postings, find decision makers, discover emails, generate personalized outreach, and push leads to Instantly campaigns. All code lives under `.claude/` (skills, agents, auth, env). There are no top-level source files.

**A Google Sheet is the database.** Almost every script reads a sheet, enriches rows in place, and writes back — there is no local model layer, no ORM, no intermediate store. That is why column constants, batch-of-10 writes, and idempotent skip-if-filled logic carry so much weight below. The exceptions are `nppes-new-clinics` and `production-house-leads`, which use SQLite upstream — though both then build sheets and work them like the rest.

## All Pipelines

Each pipeline is a Claude Code skill with its own `SKILL.md` (authoritative detail) and `scripts/` directory. Exceptions: `healthcare-staffing-enrichment`, `recruitment-email-gen`, and `sba-campaigns` have no `SKILL.md` — their sections below plus script docstrings are the reference.

| Skill | Source | Niche | Geography |
|-------|--------|-------|-----------|
| `scrape-hr-leads` | TheirStack | HR roles | US |
| `scrape-tech-leads` | TheirStack | Tech roles | Europe + Gulf |
| `hr-leads-indeed` | Indeed (Apify) | HR roles | US states |
| `hr-linkedin-leads` | LinkedIn (Apify) | HR specialist roles (30-45 day pain window) | US cities |
| `tech-leads-indeed` | Indeed (Apify) | Engineering roles | Pan-EU cities |
| `civil-engineering-leads-indeed` | Indeed (Apify) | Civil/construction roles | UK cities |
| `healthcare-linkedin-leads` | LinkedIn (Apify) | Nurse Practitioner (low-applicant signal) | NY + MD |
| `verify-leads` | Any sheet | Re-verify DMs + emails | Any |
| `sba-campaigns` | SBA data | Borrower + lender outreach | US |
| `healthcare-staffing-enrichment` | Any sheet | Classify/enrich healthcare staffing agencies | US |
| `recruitment-email-gen` | Any sheet | Niche-agnostic supply-side outreach to recruitment agencies | Any (currently AU) |
| `healthcare-demand-pipeline` | Indeed (Apify) | Clinical roles — ask Jude for position types per client | Ask Jude for states per client (Indiana LIVE; Texas + Florida clones exist) |
| `apollo-dm-waterfall` | Any sheet | DM discovery + verified email (~1 AMF credit, 0 Apollo credits) | Any |
| `exa-website-enrichment` | Any sheet | Company domain resolution via Exa (proof-on-page gate) + last-resort DM name discovery | Any |
| `personalized-icebreakers` | Any sheet | Deep-research retarget campaign (LinkedIn + site → icebreaker → body → push) | Any |
| `nppes-new-clinics` | CMS NPPES bulk files | Newly-registered medical practices (pre-job-ad demand) | All 50 states + DC, filtered at export |
| `production-house-leads` | Google Maps (Apify) | Commercial video production houses (supply side of the production lane) | LA, NYC, US secondary hubs, Toronto, London, Amsterdam, Berlin |

Utilities: `casualize-names`, `instantly-autoreply`, `add-webhook`, `local-server`. (`classify-leads` and `scrape-leads` are empty leftover directories — ignore them.)

**Skills do not own all their phases.** `hr-linkedin-leads` ships only 3 scripts (`scrape_and_pull.py`, `pull_dataset.py`, `enrich_company_profiles.py`) — everything from phase 1.5 onward is run out of `hr-leads-indeed/scripts/`, because both write the same 29-col schema. `healthcare-linkedin-leads` does the same. Calling another skill's script against your sheet is the normal pattern here, not a smell; check the SKILL.md's phase table for which directory each phase actually lives in.

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

**`healthcare-demand-pipeline` replaces the retired `healthcare-leads-indeed` skill** (that directory is gone). Only five shared scripts survived the move into `healthcare-demand-pipeline/scripts/`: `scrape_and_pull.py`, `pull_dataset.py`, `reingest_from_apify.py`, `find_company_domains.py`, `verify_dms.py`. **The skeleton table above largely does not apply to it** — it has no `classify_companies.py`, `dedupe_by_company.py`, `ai_filter_jobs.py`, `find_dm.py`, or `enrich_emails.py` of its own, and there is no Phase 3: emails come out of the Apollo waterfall. Its real sequence is 1 → 1.5 → 1.9 → 2 → 2.5 → 2.9 → 2.9b → 4 → 5, documented in its `SKILL.md`. Specifically: Phase 1.5 `process_city_scrape.py` scripts the old manual filter steps (35-day window FIRST, then the 500-employee cap, then dedupe, then conservative classify); Phase 2 DM discovery runs through the **`apollo-dm-waterfall` skill**, not `find_dm.py`; Phases 4/5 are `generate_healthcare_demand.py` (copy templates live at the top of the script and are swapped per A/B test — never treat current copy as permanent) and `push_healthcare_demand.py`.

**Per-client copy scripts are cloned, never parameterized.** When a second client needs different copy on the same pipeline, the generate/push pair is duplicated under a new name rather than branched with a flag — `generate_healthcare_demand.py`/`push_healthcare_demand.py` feed the LIVE Indiana campaign and must not be edited for another client; `generate_texas_demand.py`/`push_texas_demand.py` are the Texas clone (different copy, 5 slots, no persona/age-band routing, own tab); `generate_florida_demand.py`/`push_florida_demand.py` are the Florida clone (same client as Texas, copy approved 2026-08-01, 4 slots, tab `Leads`). The same convention produced `push_campaign_uk.py` and `generate_emails_uk.py` in `.claude/scripts/`. Clone; do not retrofit. The clones drift structurally too — on the Texas sheet AD is "Keep Reason" (a DM-title adjudication pass) and the generation audit trail starts at AE; on the Florida sheet AD holds the waterfall's `dm_status` (different vocabulary from AB elsewhere) and is never written by the generator, with the audit trail at AE-AJ. Florida's generator also **skips** rows where `employer_type` would fall back to the generic "healthcare employers" instead of sending a bland line — read the docstring at the top of each clone before touching it; the copy rules (single vs double newline spacing, historical-only claims, no bench claim) are deliberate and documented there.

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

## Verification — There Is No Test Suite

No `requirements.txt`, `pyproject.toml`, test files, linter config, or CI exist. Dependencies are whatever is installed in the ambient `python3`. **Do not invent build/lint/test commands** — verification happens by running scripts against real sheets with their safety flags:

| Flag | Meaning |
|------|---------|
| `--dry_run` | Print what would change; write nothing. Default posture for any destructive or costly step. |
| `--preview N` | Render N generated outputs (email bodies, classifications) to stdout for human approval. Required before Phase 4. |
| `--limit N` | Cap rows processed — always use a small N on the first run of a repurposed script. |
| `--apply` | Actually commit deletions/overwrites. **Never pass without asking Jude first.** |
| `--tab` | Target sheet tab (multi-tab skills: `healthcare-staffing-enrichment`, some utilities). |

Because every script is idempotent and skips already-processed rows (see Batch-of-10 below), the safe verification loop is: `--dry_run` → `--limit 5` real run → inspect the sheet → full run.

Two traps in that loop: `--limit N` counts **pending** rows, not sheet rows, so on a partially processed sheet it lands wherever the next N unfilled rows are; and idempotence keys off a specific output cell being non-empty, so re-running after a partial failure resumes rather than repeats — to actually regenerate a row you must clear its output cell first.

## Critical Rules

**Batch-of-10:** Every script MUST write to the Google Sheet after each batch of 10 rows — never batch all then write. This enables crash recovery and idempotent reruns (scripts skip already-processed rows).

**Geography is Jude's call:** before scraping any new vertical/client, ask which state(s) to target. Scrape city-grids (metros + regional hubs), not state-level queries — city grids return ~2x the postings.

**35-day window + 500-employee cap (healthcare-demand-pipeline):** postings older than 35 days never land on a campaign sheet; the window applies BEFORE dedupe and enrichment. Dedupe winner = oldest posting inside the window. Cut from 60 to 35 on 2026-07-31 on measured reply data (≤30 days old at first contact → 2.35%; 31-60 → 0.88%; >60 → 0.00% from 46 sends). 35 rather than 30 gives the ~1-week sequence headroom. **Do not narrow it to a 25-35 band** — the fresh end carries the result (0-25 days replies at 2.42%, a 25-35 band alone at 1.23%).

**Live campaigns are frozen:** never modify an active Instantly campaign (sequence, leads, copy) or the enriched sheet rows feeding it without Jude's explicit instruction.

**Instantly custom variables:** send as `custom_variables` on POST/PATCH /leads (merges into stored payload). Nesting under `payload` gets silently replaced; loose top-level keys are dropped. Verify persistence with a fresh GET, not the write response.

**Apollo (Basic plan):** People Search (`mixed_people/api_search`, x-api-key header) is the only free endpoint — names come back obfuscated (`Wo***e` = 2 prefix letters + 3 literal asterisks + last letter), no emails/LinkedIn/locations. Old `mixed_people/search` path 403s. Apollo = DM identification only; AMF = all emails (only charges on found verified emails). The `apollo-dm-waterfall` skill implements the full flow.

**Email template approval gate:** Phase 4 (`generate_emails.py`) MUST NOT run until the user has seen and approved the template. Show `--preview N` output first, wait for explicit approval, then run for real.

**Phase 4 and Phase 5 are manual stops** — never auto-chain into them.

**Valid emails only:** AnyMail Finder `risky` results are rejected everywhere — only `email_status == "valid"` emails are written to sheets or pushed to Instantly, and DM name/title/LinkedIn are never written without a valid email (no partial data).

**No sending accounts:** Never add sending accounts to Instantly campaigns; Jude configures those manually in the UI.

**Casualization is embedded in every GTM generator:** first names (common nicknames only: William→Will), company names (strip legal suffixes/generic tails), cities (local nicknames: Indianapolis→Indy). Canonical rules live in the `casualize-names` skill; each pipeline applies them at generation time (LLM prompt rules or the shared `NICKNAMES` map). Any NEW outreach generator must include them.

**Text-only campaigns:** New Instantly campaigns set `text_only: true` and `first_email_text_only: true` — those flags are what make the send plain text. Personalization values written to leads are plain text with newlines and no markup. Sequence step bodies still carry the thin `<div>{{personalization}}</div>` / `<br />` wrapper Instantly stores them in; that is expected and is not the HTML the rule is about.

**Never delete rows without asking** — always confirm before calling `--apply` on destructive steps.

## DM Targeting Rules

Rules are pipeline-specific; see each SKILL.md. Summary:

| Pipeline | DM Target Logic |
|----------|----------------|
| `hr-leads-indeed` / `hr-linkedin-leads` | Size-based: CEO (<50), HR Manager (50-200), VP HR (200-500). Senior role (CHRO/VP HR) → always CEO |
| `tech-leads-indeed` | 3-pass: CTO/VP Eng → CEO/Founder → Head of People (safety net) |
| `civil-engineering-leads-indeed` | 2-pass: Owner/MD/CEO (<50) or COO/Ops Director (50-200) → fallback |
| `healthcare-demand-pipeline` | **Fixed ladder: CEO → COO → Medical Director → nobody** (Jude, 2026-07-31). Lists capped at 500 employees. Only rung 1 is evidence-backed; 2 and 3 are coverage fallbacks. Banned: HR at any level, every other clinical title, site/regional ops, practice managers. No one findable → leave the row un-enriched |
| `healthcare-linkedin-leads` | 3-pass fixed: Practice/Office Manager → Owner/Medical Director/CEO → Managing Partner (legacy routing) |

### Measured DM evidence (healthcare demand, 4 campaigns, 1,209 leads, Jul 2026)

The owner-first rule is not a heuristic — it is the only DM finding in this repo backed by campaign data. Pooled across Indiana, Texas and the two June 2nd campaigns:

| Title targeted | Emailed | Replies | Rate |
|---|---|---|---|
| Owner / CEO / Founder | 608 | 17 | **2.80%** |
| Clinical leader (all levels) | 307 | 4 | 1.30% |
| COO / Ops / Administrator | 87 | 0 | 0.00% |
| HR / Talent (incl. CHRO) | 32 | 0 | 0.00% |
| Other / unclear | 173 | 0 | 0.00% |

The DM rule that came out of this (Jude, 2026-07-31) is a fixed ladder: **CEO → COO → Medical Director → nobody.** Only the first rung is a finding. Rungs 2 and 3 are coverage fallbacks Jude chose for when no CEO exists, and both are unsupported by the data — COO/Ops is 0 for 87 and clinical leadership produced zero interested replies from 307. The Medical Director rung is restricted to a genuine company-level one, since at small clinics that title is usually a contracted outside physician. Clinical leadership is otherwise banned entirely: chief-level went 0 for 126 (CMO 0/78, CNO and Director of Nursing 0/29, Chief Clinical Officer 0/19), and the old discipline-matching ladder (CMO→physician roles, CNO→nursing) is deleted — do not reconstruct it. A company with nobody on the ladder is left un-enriched, because a wasted AMF credit plus a burned company beats an empty row.

**Company size is the other half of the story** (Indiana + Texas, size taken as the lower bound of the sheet's Company Size range):

| Size band | Emailed | Replies | Rate | Interested |
|---|---|---|---|---|
| TINY <50 | 142 | 5 | **3.52%** | 1 |
| size unknown | 219 | 6 | 2.74% | 1 |
| MID 50-499 | 249 | 3 | 1.20% | 1 |
| **LARGE 500+** | **251** | **0** | **0.00%** | **0** |

**251 companies at 500+ employees produced zero replies of any kind** — not one interested, not one rejection, nothing (2.30% for everything under 500; Fisher p = 0.008). That band is 29% of the list.

⚠️ **Size and title were confounded in that measurement** — under the old clinical-first rule the 500+ rows were targeted 175 clinical / 22 HR / 41 other and only **2** owner-ish contacts (a Managing Partner and a Managing Director, no actual CEO), while under-500 rows got 194 owner vs 89 clinical. So a large-org CEO was never actually tried, and strictly what the zero disproves is clinical-and-HR-at-big-orgs. **Jude capped at 500 anyway on 2026-07-31**, accepting that trade rather than spending another campaign to separate the two.

The 500+ band was also where list quality was worst: individual nursing homes inherit their parent chain's headcount (nine separate Life Care Center facilities all tagged 40,000), and non-healthcare corporates and government bodies survive classification there (Wolters Kluwer, Hearst, SpartanNash, SGS, US Dept of Veterans Affairs). The cap removes those chain facilities too — an accepted cost, surfaced in Phase 1.5's output rather than dropped silently.

**HQ vs regional at multi-site chains is an open question.** Company-level titles replied at 2.03% (10/492), region-scoped at 3.70% (1/27, and that one reply was "Stop."), site/facility-scoped at 0.00% (0/40). Region + site pooled is 1/67 against 10/492 company-level, Fisher p = 0.61 — no signal either way. Nobody has enough data; it needs a deliberate split test, not a guess.

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
- Apollo waterfall scripts also need: `APOLLO_API_KEY`
- `exa-website-enrichment` scripts also need: `EXA_API_KEY`
- `nppes-new-clinics` phases 1-3 need no API key (CMS data is free), only the Google OAuth token for `export_leads.py --to_sheet`. Its campaign track (below) needs `ANYMAILFINDER_API_KEY`, `PURPLE_MAGIC_KEY` (Purple Magic / ConnectorOS — `find_dm_waterfall.py`, `pm_rescue.py`), the Azure OpenAI vars, and `APIFY_API_TOKEN`
- Google Sheets OAuth: `.claude/token.json` (setup via `.claude/setup_google_auth.py`)

Gitignored and therefore absent on a fresh clone: `.claude/.env`, `.claude/token.json`, `.claude/scripts/` (see below), and `.claude/skills/nppes-new-clinics/data/` (the SQLite store, cached CMS zips, and exports).

## API Quirks

- **Apify LinkedIn scraper (employees):** Sync endpoint returns HTTP **201**. Actor: `harvestapi~linkedin-company-employees`. Title is in `currentPositions[0]["title"]`.
- **Apify LinkedIn jobs scraper:** `insight_api_labs~linkedin-jobs-scraper` — HTTP 201; no company size in response; `fewApplicants=True` for low-applicant pain filter.
- **Apify LinkedIn profile scraper:** `dev_fusion/Linkedin-Profile-Scraper` — used in Phase 2.5 DM verification.
- **Apify LinkedIn company profile:** `pratikdani~linkedin-company-profile-scraper` — fills website/size/description for LinkedIn-sourced pipelines.
- **AnyMail Finder:** Auth header is `Authorization: {API_KEY}` (no "Bearer"). Two endpoints: `/find-email/person` and `/find-email/decision-maker`. Valid categories: `ceo, engineering, finance, hr, it, logistics, marketing, operations, buyer, sales` (`coo` is NOT valid — use `operations`).
- **Instantly API v2:** Bearer token auth. Leads added one at a time (no bulk endpoint). `DELETE /leads/{id}` is safe; `DELETE /leads` with a body wipes the **entire campaign**.
- **Purple Magic (ConnectorOS):** second-lane email provider — base `https://api.connector-os.com/api/email/v2`, Bearer auth (`PURPLE_MAGIC_KEY`). `/find` takes `{firstName,lastName,domain}`; `/decision-makers` takes `{domain}` and returns nobody ~83% of the time on small practices. Different providers fail on different companies, which is why it's run over AMF `not_found` piles rather than instead of AMF.
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

**healthcare-demand-pipeline** extends the base schema with AB:dm_status, AC:Indeed URL (`https://www.indeed.com/viewjob?jk={Job_Id}` — **AC always holds the Indeed URL, on every sheet; Jude's rule**), AD-AL generation audit trail (persona, age_band, cleaned_role, role_plural, team_word, employer_type, month, casual_company, review_status), AM:hq_state. **tech-leads-indeed** and **civil-engineering-leads-indeed** also have minor column differences — each SKILL.md has the verified layout.

**`tech-leads-indeed`** adds `AB:template_variant` and `AC:cleaned_role` (populated by `generate_emails.py`).

⚠️ Always check `COL_*` constants at the top of a script before running it against a sheet — repurposed scripts with shifted columns have corrupted data before.

## Shared Utility Scripts

`.claude/scripts/` contains one-off and cross-pipeline utilities (not part of any skill's standard pipeline). **This directory is gitignored** — it exists only on Jude's machine and will be absent on a fresh clone, so never assume these scripts are present; check before referencing one. Current local contents:
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

Standalone enrichment **and outreach** skill for healthcare staffing agency sheets (the supply side of the healthcare pipeline). No SKILL.md — script docstrings are the reference. Uses Azure OpenAI GPT-4.1 throughout (classification, website picking, about-page summaries).

**Enrichment order:** `classify_agencies.py` (Google-via-Apify + GPT-4.1; `--apply` deletes non-agencies) → `find_websites.py` / `find_missing_websites.py` → `verify_websites.py` / `reverify_websites.py` (label website correct/not_correct; reverify clears DM columns when the stored website turns out wrong) → `scrape_company_about.py` → `enrich_linkedin_company.py` / `enrich_company_profiles.py` → `find_ceo.py` (AMF only, no Google fallback; rejects hosting-platform domains like Squarespace/Wix and emails whose domain doesn't match the company).

**Outreach tail:** `generate_icebreaker.py` → `generate_email_body.py` (fixed body template + icebreaker) → `push_campaign.py` (creates the Instantly campaign as **DRAFT**; Jude activates manually). These take `--tab` — the sheet is multi-tab.

**Demand-campaign track** (healthcare recruitment firms as the leads, sourced from an AI Ark export with an A-N schema): `split_by_headcount.py` (splits source into `1-50 EMP` / `50-200 EMP` tabs by col B) → `find_ceo_demand.py` (AMF /decision-maker with domain + company name; appends O:dm_name P:dm_title Q:dm_email R:dm_linkedin S:email_status) → `split_dm_names.py` (GPT-4.1 name split → T/U) → `generate_demand_body.py` (fixed plain-text template, `{first_name}` only → V) → `push_demand_campaign.py` (DRAFT campaign, text-only, daily_limit 500; refuses rows whose email domain doesn't match the website domain; skips duplicate emails both within the push and against rows already pushed anywhere in the tab; leads rejected by Instantly's workspace blocklist get col W = `BLOCKLISTED` and are never retried — resume skips both `TRUE` and `BLOCKLISTED`) → `patch_greeting.py` (one-off post-push copy patcher — updates sheet col V, the Instantly sequence, AND each pushed lead's personalization).

**Expansion-campaign track (Aug 2026 — cloned from the demand track, never parameterized):** a second angle over the same AI Ark firms, framing newly-opened clinics staffing up new locations. Two additions to the pattern above. (1) A **Purple Magic email lane**: `find_ceo_pm_demand.py` is the PM-primary twin of `find_ceo_demand.py` (AMF lane) — `/decision-makers {domain}` → positive owner-like title gate BEFORE the `/find` call → valid + domain-matched email only, appending the same O-S columns but stamping `pm_*` statuses so the AMF and PM lanes stay distinguishable. (2) Cloned copy scripts: `generate_expansion_body.py` (Jude's verbatim expansion template, `{first}`/`{company}` only → col V) and `push_expansion_campaign.py` (DRAFT clone of `push_demand_campaign.py` mirroring the May 25th Supply campaign — subject `Awesome work at {{companyName}}`, "Hi" greetings, "Sent from my iPhone" footer, delays 2/2/5). **Do not edit `generate_demand_body.py`/`push_demand_campaign.py`** — they belong to the completed 1-50 demand campaign.

**SIA one-offs:** `enrich_sia_emails.py` (AMF person endpoint), `enrich_sia_company_dms.py` (AMF /decision-maker for company-only rows), `rescue_sia_dms.py` (Google-search DM discovery then AMF person) — hardwired to the SIA Attendees sheet's own A-L schema.

**Quirks:**
- Every script defaults to a hardcoded `SHEET_ID`; `verify_websites.py`, `reverify_websites.py`, and `enrich_company_profiles.py` take no `--sheet_url` at all — they only run against that sheet.
- **Three conflicting column schemas coexist in this skill.** Older scripts (`reverify_websites.py`) expect DM name in col F; the supply-side outreach tail (`find_ceo.py` onward) uses N:dm_name, P:dm_email, Q:dm_linkedin, R:email_status; the demand track uses O:dm_name, Q:dm_email, S:email_status on top of the A-N AI Ark layout. Always check the `COL_*` constants at the top of a script before running it.
- Status filters differ per track — some downstream scripts filter on `email_status == "found"`, not `"valid"`. Match the docstring of the script you're running.

## recruitment-email-gen

Niche-agnostic supply-side outreach skill (ICP = recruitment agencies; first used for the AU campaign). No SKILL.md — script docstrings are the reference. Unlike `verify-leads`, column flags take **letter names** (`--col_website J`, `--col_dm_name AA`), and every column is configurable per run, so it works against any sheet schema.

**Order:** `find_dm_amf.py` (AMF /decision-maker straight from domain — no Google DM search; valid + domain-match emails only) → `scrape_website.py` (direct HTTP fetch of about/services/sectors pages, GPT-4.1 summary — no Apify cost) → `generate_icebreaker.py` (static icebreaker, only first name injected; also splits first/last name) → `generate_email_body.py` (GPT-5.1 extracts just two facts — ICP + one role — and the email is assembled deterministically in code) → `push_campaign.py` (DRAFT campaign; body rides as `{{personalization}}`; no subject line).

## apollo-dm-waterfall

Niche-agnostic DM discovery + verified email for **~1 AMF credit and 0 Apollo credits** per company — replaces AMF /decision-maker (2 credits, no title/LinkedIn). Its `SKILL.md` has exact commands; column letters are all CLI flags so it runs against any sheet schema. Waterfall per row: free Apollo People Search by domain (LARGE orgs searched with a `person_titles` filter) → GPT-4.1 ranks top 3 candidates by budget authority (`RANK_SYSTEM` in `apollo_dm_waterfall.py` is the only niche-specific part — edit it to retarget) → per candidate: Google de-obfuscation of the name → AMF person endpoint → "{first} {last-initial}" retry → sheet-CEO rescue (TINY/MID orgs only). Writes a status column; never writes DM data without a valid email.

Companion scripts: `amf_ceo_rescue.py` (AMF /decision-maker `ceo` rescue for TINY rows where Apollo had no people; 2 credits per found), `amf_dm_fallback.py` (AMF /decision-maker for `not_found` rows — TINY/MID→ceo, LARGE→hr; **standing rule: always run after waterfall + rescue**), and `apollo_org_enrich.py` (Apollo org enrichment for blank-size rows → real headcount in col M + HQ state in col AM; ~1 Apollo credit per company; needs `APOLLO_API_KEY`).

Two newer companions are **not yet in the SKILL.md** — docstrings are the reference: `amf_person_fill.py` (person-endpoint email fill for rows that already have a DM name, e.g. after the waterfall's identity-only `--skip_email` mode or `find_dm_exa.py` — 1 credit vs 2 for re-resolving a role; rejects emails whose domain doesn't match the company, which caught ~29% wrong-person hits on initial-only finds) and `amf_ceo_then_ops.py` (sequenced /decision-maker pass, Jude 2026-08-01: `ceo` first — a hit upgrades an existing Apollo admin, a miss never downgrades one — then `operations` only for rows still empty; reaches the companies Apollo has nobody for).

## exa-website-enrichment

Domain resolution with a proof-on-page gate — its `SKILL.md` is authoritative for `enrich_websites_exa.py` (acceptance ladder, rejected-domain classes, ~$0.007/company cost, `EXA_API_KEY`). Run it BEFORE any DM/email enrichment when websites are missing or untrusted: a wrong domain doesn't fail loudly, it emails a real person at the wrong company. Never writes a domain it can't prove; blank beats wrong.

The skill also ships `find_dm_exa.py`, which the SKILL.md does **not** cover (its "ends at column L" claim predates it): a last-resort DM **name** finder for companies both Apollo and AMF /decision-maker dead-end on. GPT-4.1 extracts a name+title from Exa results under the CEO→COO→Medical Director ladder; it writes a name only, never an email — complete the row with `amf_person_fill.py`.

Two behaviors added Aug 2026 (ahead of the SKILL.md):
- **Junk-domain classes grew from live failures on NPPES-sourced lists.** NPI-registry mirrors are the worst false positive on a list sourced *from* the NPI registry — the page names the practice, city, and taxonomy, so it passes content verification perfectly while being a directory (12 of 789 resolved domains on the first healthcare run). Senior-care referral directories are the same trap for home-care agencies (CAREGIVERS ON DEMAND resolved to aplaceformom.com, and enrichment then returned that directory's CEO). Secretary-of-State registry mirrors are matched by a regex family (`JUNK_HOST_RE`, rejection class `registry_mirror`), not a fixed list.
- **Prior-attempt rows are skipped by default.** A miss leaves the website cell blank but stamps an `exa_*` status; without the skip those rows sit at the top and get re-searched (re-billed) on every `--limit` run before any fresh row is reached. Pass `--retry_attempted` to deliberately redo them.

## personalized-icebreakers

Despite the name, this is a **complete retarget campaign pipeline**, not just an icebreaker generator — deep per-lead research (LinkedIn + whole-site crawl) → dossier → icebreaker → full body → its own Instantly push. Built July 2026 for the healthcare retarget. `SKILL.md` has exact commands and the full copy-rule list; `reply-playbook.md` holds Jude's reply ladder for the campaign. All column letters are CLI flags, so it runs against any sheet schema. All LLM calls are Azure OpenAI GPT-4.1.

**This is the personalized alternative to `recruitment-email-gen`'s static icebreaker** — pick one per campaign, not both.

| Phase | Script | Purpose |
|-------|--------|---------|
| 0 | `scrape_linkedin.py` | DM's LinkedIn profile → compacted JSON (dev_fusion actor, $3/1k) |
| 1 | `scrape_facts.py` | Whole-site crawl → **per-page** abstracts as JSON (direct HTTP, no Apify cost) |
| 2 | `build_dossier.py` | Merge both sources → summary, niche, `healthcare_fit`, best/second fact + when-tags, flags |
| 3 | `generate_icebreaker.py` | n=3 → gates → reviewer → verify-revise loop → the line, or empty |
| 4 | `generate_body.py` | Greeting + icebreaker + Jude's fixed offer template, routed by `healthcare_fit` |
| 5 | `push_retarget.py` | DRAFT Instantly campaign, subject "new reqs", body rides as `{{personalization}}` |

Phases are split so each is re-runnable alone — retune copy by re-running phase 3 only, no re-scraping. Every script is batch-of-10 and resume-safe (skips filled output cells).

- **LinkedIn is the highest-yield source, not the website.** ~35% of agency sites WAF-block even with a full Chrome header set; a profile that scrapes always carries tenure, and `about` holds founder stories and self-published numbers. ~25-30% of profiles come back blocked per pass — recover by simply re-running. Phase 1 crawls the *whole* site (25 pages / ~14k chars / 75s bounds, priority-scored: team/story first) and emits per-page abstracts, never one concatenated blob — the homepage headline drowns the buried details that are the entire point.
- **`healthcare_fit` (from the dossier) routes the copy**, and **LinkedIn overrides the sheet as source of truth** — profiles routinely show the lead has moved employer since the list was built. That sets a `MOVED->{company}` flag; `NOT_A_RECRUITER` is the other flag. Both are skipped downstream, and `MOVED` rows are never pushed (dead email).
- **Fact priority:** prior career > published numbers > awards > milestone > narrow specialism. **Never education**, in either half. Business model/structure (locums vs perm, direct hire only, headcount-as-commentary), self-classification from directory categories, and values/culture/mission praise are banned fact types. So is surveillance material (posted pay rates, registered entity names, HQ locations) — filtered mechanically at both fact-input and line-output level.
- **The v3 formula is locked (July 2026):** `Love {specific 1}, {compliment tied to specific 1}. Btw, also noticed/saw how/that {specific 2}.` The compliment must credit them and be safe if slightly wrong; general industry truths are fine, guesses about their situation ("you must be struggling to fill those") are not — "must" is mechanically banned. Openers are Love-family only. Company names and acronyms are deliberately lowercase (correct branding everywhere is an AI tell).
- **Designed to return nothing rather than fake-personalize.** Never-converged rows write an empty cell; a `--static_fallback` flag exists but the default posture is empty, and **rows with no icebreaker are skipped by phase 4 and never sent** — the campaign is a personalization-only experiment. Expect a real miss rate.
- Phase 4's offer copy is **Jude's template verbatim** (stored at the top of the script, swapped per A/B test). Only `{icp}` and `{roles}` may change; CTA, proof line, and sign-off are fixed.
- Phases 3 and 4 are subject to the same preview-and-approve gate as any email generation step.

## nppes-new-clinics

**The first pipeline that is not a Google-Sheets pipeline** (`production-house-leads` later adopted the same model). Everything else in this repo treats a Sheet as the database and enriches rows in place. This one ingests CMS NPPES bulk files into **SQLite** (`data/nppes.db`, WAL, gitignored) and only emits a Sheet/CSV at the export step. Sources demand *before* a practice posts a job ad: a new organization NPI lands 3-8 months before an insurance-accepting clinic opens, 1-3 months before its first staff ad. `SKILL.md` is detailed and authoritative — read it before touching this skill.

| Phase | Script | Purpose |
|-------|--------|---------|
| 1 | `pull_new_practices.py` | Weekly V2 zip → filter (org + window + state + taxonomy) → SQLite |
| 1.5 | `build_baseline.py` | Monthly full file (~1.1GB) → address/name novelty baseline; rebuild monthly |
| 2 | `classify_practices.py` | NEW_INDEPENDENT / NEW_LOCATION / LIKELY_ADMIN / UNCERTAIN + solo-PLLC flag + score |
| 3 | `export_leads.py` | Scored CSV + optional Sheet; `--states IN,TX` filters to a client's geography |
| util | `resync_store.py` | Re-apply current allowlist/normalization to already-stored rows |

Architectural differences that will bite if assumed away:

- **Config-driven, never hardcoded** — `config/settings.json` (states, window, scoring weights) and `config/taxonomy_allowlist.json` (volume-first broad include + `_denylist`). Edit config, not scripts.
- **Pull skips known NPIs, so config changes never self-correct stored rows.** After tuning the allowlist or normalization you MUST run `resync_store.py`, or the DB keeps decisions made under the old config.
- **Exports are deltas by default** (`exported_at IS NULL`). `--include_exported` re-exports everything; `--mark_contacted npis.txt` retires worked leads.
- **Filter on Provider Enumeration Date, never on file presence** — weekly files mix new enumerations with updates and deactivation stubs (blank Entity Type Code; drop those first). The first weekly after a monthly release is ~4x normal size from update bloat, not new orgs.
- **The NPPES API cannot substitute for the bulk files** — no enumeration-date filter (params silently ignored), hard ~1,200-row ceiling with silent duplicate pages past it. API is for per-NPI lookups only.
- **~45-50% of new org NPIs are solo-clinician PLLCs**, not staffing launches. The solo flag is a score penalty, not a drop. Say "registered", never "opened" — and don't personalize on the practice address, which is often the owner's home.
- **Validation gate before any enrichment spend:** hand-check 20-30 NEW_INDEPENDENT records with Jude first. (The v1 "enrichment stays out of scope" rule is retired — the campaign track below now carries enrichment and copy in-skill.)
- National by design (~950/week raw allowlisted). Single states are too thin to scrape alone (IN ~11/week) — always run national, filter at export.
- **Multi-site owner signal:** one Authorized Official holding several new NPIs is a group opening locations, i.e. hot demand. `owner_site_count` is computed across the whole store in `resync_store.py` and scored `+multi_site_owner` only inside the 2-9 band — past ~10 sites it's an enterprise system (Cleveland Clinic's CFO holds 138), MSP-gated, not a warm lead. This makes `resync_store.py` matter for scoring, not just for allowlist changes.

### Campaign-execution track (Jul-Aug 2026 — NOT in the SKILL.md; docstrings are the reference)

The store → campaign sheet → verified email → copy path, built as untracked scripts in the same `scripts/` dir. Proven logic from other skills is reused **by import**, never edited — `resolve_domains_batch.py` and `resolve_parent_domains.py` both `importlib`-load `healthcare-demand-pipeline/scripts/find_company_domains.py`, which feeds the LIVE Indiana pipeline and must not be touched.

| Step | Script | Purpose |
|------|--------|---------|
| Sheet | `build_campaign_sheet.py` | Older category-priority sheet (psychiatry/behavioral first; `mental_health_counseling` excluded — 1099-therapist practices don't pay placement fees). Random within categories, one row per OWNER, solo/LIKELY_ADMIN excluded |
| Sheet | `build_commercial_sheet.py` | THE current sheet (scope agreed 2026-08-04): full commercial pool — multi-staff clinics, provider practices, home-care/nursing agencies, facilities IN; solo PLLCs and non-clinical social services OUT. Facility Type (col B) is the per-CODE NUCC display name (never per-prefix — a prefix bucket once swept in a horse stable). Fully shuffled by explicit instruction so reply data, not ordering, decides what works |
| Domains | `resolve_domains_batch.py` | Same Google-via-Apify + LLM pick as Indiana's `find_company_domains.py`, plus one behavior: misses stamp AB=`fcd_no_match` so reruns reach fresh rows (the original re-Googles the same failures: 400 lookups → 7 new domains on pass two) |
| Domains | `resolve_parent_domains.py` | For shell-LLC rows ("FAIRVIEW OPCO LLC"), search the Parent Org LBN (col J) instead; writes the parent domain with AB=`parent_domain` — for a health-system site the parent is where buying power lives |
| Email | `find_ceo_emails.py` | Campaign-sheet layout. AO title owner-like → AMF person (1 credit); else AMF /decision-maker `ceo` (2 credits). `--target N` stops once N valid emails exist |
| Email | `find_dm_waterfall.py` | Commercial-sheet layout. **The filer is the target — deterministically** (the docstring still describes an LLM gate; the in-code comments dated 2026-08-04 supersede it: the gate was tried and wrongly rejected COOs and Office Managers — filing an NPI is itself evidence of authority). Only support-function titles (`NOT_TARGET` regex: finance, legal, billing, IT, marketing, front desk…) fall through to PM /decision-makers + GPT-5.1 ranking, with a **positive gate** on ranked titles (unknown fails; "Assistant to CEO" can't ride the word CEO through). Col AI records which lane won |
| Email | `pm_rescue.py` | Purple Magic second lane over AMF `not_found` rows — AMF misses ~65% here because practices registered 30-90 days ago barely exist on the web yet |
| Copy | `generate_connector_emails.py` | Jude's verbatim templates: Variant A (new practice) / Variant B (new location). `{ptype}`/`{gtype}` come from a fixed NUCC-code map — deterministic, no LLM; generic codes fall back to Jude's generic wording. Casualization embedded; bodies → col Z; `--preview` approval gate applies |
| Push | `push_connector_campaign.py` | Phase 5 — DRAFT Instantly campaign "New Clinics Connector - Aug 2026" (tab `Leads`). Per-variant subject via `{{subject_line}}` custom var (`practice staffing` for A / `new location staffing` for B/C); 3-step sequence (day 0/2/5, steps 2-3 blank-subject); signature lives in the SEQUENCE (`{{sendingAccountFirstName}}` + "Sent from my iPhone"), never in the per-lead body. One lead per unique inbox (first row wins, siblings marked DUP), text-only, no sending accounts, blocklist rejections marked `BLOCKLISTED` and never retried |

Both sheet builders deliberately place company/website/city/state/status at K/L/R/S/AB — `exa-website-enrichment`'s default flags — so that skill runs against them unmodified. All the standing email rules apply: valid-only, email domain must match the resolved website, free mailboxes rejected, never a name without an email.

## Agents

| Agent | Purpose | Model |
|-------|---------|-------|
| `decision-maker` | Research companies and identify DMs with budget authority | Sonnet |
| `code-reviewer` | Unbiased code review (correctness, performance, security) | Sonnet |
| `email-classifier` | Classify Gmail into Action Required / Waiting On / Reference | Sonnet |
| `qa` | Generate tests, run them, report pass/fail | Sonnet |
| `research` | Deep investigation with web + file access | Sonnet |
