# healthcare-demand-pipeline

THE healthcare demand-side campaign pipeline (replaces the retired
healthcare-leads-indeed skill — its shared scripts now live here):
scrape shortage-role job postings city-by-city → 35-day freshness window →
DM discovery via the Apollo waterfall → persona/age-personalized copy with a
reviewer agent → push to an Instantly campaign. First run: Indiana client,
July 2026 (1,022 companies → 565 verified DMs → 527 emails → 445 pushed).

Reference campaign sheet: "Healthcare Indiana Indeed Leads"
(1nqYLo9VVhoi-miF5-JQcvr1pyRWXEuywfnVY48aB3DM). Copy source of truth: Google
Doc "Healthcare Campaign Copy Framework" (1Yk0aSxWqO5e__uCRmLOL0eB8_4lz8rSIEEbI900-Lc0).

---

## Hard rules (violating these has burned us)

0. **Ask Jude two questions before ANY new scrape: (1) which state(s),
   (2) which position types.** Never assume or reuse the last campaign's
   geography or roles. Jude names the states and the positions; derive the
   city grid (major metros + regional hubs) and the Indeed keywords from
   them, and confirm both before running. Never expand keywords beyond what
   he approved.
0b. **Never modify a live campaign** (its sequence, leads, copy, or the
   enriched rows feeding it) without Jude's explicit instruction. The July
   2026 Indiana campaign (f59f4fa5…) is LIVE as of 2026-07-15.
1. **35-day freshness window, applied FIRST.** No posting older than 35 days
   ever lands on the main sheet. Window before dedupe, dedupe before
   enrichment — applying it after enrichment threw away 14 paid leads once.
   Cut from 60 to 35 on 2026-07-31 on measured reply data: ≤30 days old at
   first contact replies at 2.35%, 31-60 days at 0.88%, >60 days at 0.00%
   (0 replies from 46 sends). The old 46-60 slice was 148 leads for one
   reply. 35 rather than 30 gives the ~1-week sequence headroom so the last
   touch still lands inside the productive band. **Do not narrow to a 25-35
   band** — 0-25 day postings reply at 2.42%, a 25-35 band alone at 1.23%.
2. **500-employee cap (Jude, 2026-07-31).** Applied in
   `process_city_scrape.py` right after the freshness window, and in
   `pull_dataset.py` on the fallback path. Reply rate by band on Indiana +
   Texas: TINY <50 **3.52%** (5/142), size-unknown 2.74% (6/219), MID 50-499
   1.20% (3/249), **LARGE 500+ 0.00% (0/251)**. The 500+ band was 29% of the
   list and produced nothing at all — not one interested reply, not even a
   rejection. This REPLACES the earlier no-size-filter rule.
   - **Blank/unknown size is KEPT** — those rows reply at 2.74% and are mostly
     small independents.
   - The cap uses the LOWER bound of Indeed's range, so "201 to 500" is kept.
     That matters: one of the three interested replies (Rolling Plains
     Memorial Hospital) was exactly that band.
   - **Accepted cost:** Indeed reports the employer BRAND's headcount, so a
     chain facility inherits its parent's size and the cap removes it too —
     nine Life Care Center homes on the Indiana sheet are all tagged 40,000.
     Phase 1.5 prints a sample of what the cap dropped so this is visible
     rather than silent. `--max_employees 0` disables it.
   - Because chain facilities are now filtered out upstream, the
     chain-facility regional-VP exception discussed on 2026-07-31 is moot and
     is NOT implemented.
3. **Dedupe winner = oldest posting INSIDE the window** (max pain, still live).
4. **Phase 4 and Phase 5 are manual stops.** Template approval + `--preview`
   before generation; explicit go before push.
5. **Copy is swappable per test** — templates live at the top of
   `generate_healthcare_demand.py` (MIDDLE/MIDDLE_C/CTA/openers) and mirror
   the Google Doc. Jude A/B tests copies; never treat the current copy as
   permanent. Get approval on any copy before a generation run.

## Phase sequence

| Phase | Tool | Notes |
|-------|------|-------|
| 1. Scrape | `scripts/scrape_and_pull.py` with `--keywords`/`--cities` overrides | **City-grid, never state-level** (state-level Indiana yielded 4.8k postings; 20 cities yielded 10.1k). 20 shortage keywords below. Creates/appends a RAW sheet. |
| 1.5 Process | `scripts/process_city_scrape.py --raw_sheet_url --main_sheet_url` | 35-day window → dedupe → name filter → conservative LLM classify → append new companies. `--drop_schools` optional. |
| 1.9 Domains | `scripts/find_company_domains.py --apply` | Google-via-Apify. |
| 2. DM discovery | **apollo-dm-waterfall skill** | Fixed ladder: CEO → COO → Medical Director → nobody (see below). |
| 2.5 Verify (optional) | `scripts/verify_dms.py` (dry run) | Over-flags commutes; review, never blind --apply. |
| 2.9 Rescues | waterfall skill's `amf_ceo_rescue.py` (TINY no-people rows) | 2 credits per found. |
| 2.9b Fallback | waterfall skill's `amf_dm_fallback.py` (`not_found` rows) | **STANDING RULE (Jude, 2026-07-16): always run.** AMF /decision-maker, category `ceo` at every band (LARGE→hr retired 2026-07-31, 0 replies from 32 HR contacts). 2 credits per found. |
| 4. Generate | `scripts/generate_healthcare_demand.py --preview N`, then real run | See below. APPROVAL GATE. |
| 5. Push | `scripts/push_healthcare_demand.py --campaign_id ID` | See below. EXPLICIT GO REQUIRED. |

**Reference keyword set (Indiana campaign, 20):** Radiologic/CT/MRI
Technologist, Dental Hygienist, Surgical Technologist, ICU RN, ER RN,
Operating Room RN, CRNA, Respiratory Therapist, Psychiatric Nurse
Practitioner, Pharmacist, Medical Laboratory Scientist, Physical Therapist,
Occupational Therapist, Speech Language Pathologist, Nurse Practitioner,
Physician Assistant, EMT, Paramedic. This is a REFERENCE, not a default —
the actual keywords come from the position types Jude names per client
(hard rule 0). Shortage research: imaging/dental hygiene/surg tech/specialty
RN are the strongest recruitment-fee opportunities; EMT is weak economics.

## DM targeting (implemented in the waterfall's RANK_SYSTEM)

**The ladder (Jude, 2026-07-31 — final): CEO → COO → Medical Director → nobody.**
Work down it and stop at the first person findable.

1. **CEO / Owner / Co-Owner / Founder / Co-Founder / President / Managing
   Partner / Principal.** The top of the house.
2. **COO / Chief Operating Officer** — company-level only, not a site or
   regional operations manager.
3. **Medical Director** — only a genuine employed, company-level one. At small
   clinics a standalone "Medical Director" is usually a contracted outside
   physician holding the licence for compliance, with no hiring involvement.
   Good signals: paired with an ownership or executive role ("Owner & Medical
   Director"). If you cannot tell, skip.
4. **Nobody.** Write nothing and leave the row un-enriched — a lesser contact
   spends an AMF credit, burns the company and does not reply.

**What the evidence actually supports.** Only rung 1 is a finding: pooled over
four demand campaigns (1,209 leads) Owner/CEO/Founder replied at 2.80%
(17/608) vs 0.67% for every other title combined (4.2x, Fisher p=0.004), and
**every interested reply came from an owner.** Rungs 2 and 3 are Jude's
judgement calls for coverage when no CEO exists, NOT measured wins — COO/Ops
has 0 replies from 87 contacts, and clinical leadership produced 0 interested
replies from 307. Never rank them above the CEO.

**Banned at every size:** HR at any level incl. CHRO (0/32); every clinical
title except a genuine company-level Medical Director — no CMO, CNO, Director
of Nursing, Chief Clinical Officer, Clinical Director, Director of
Rehab/Therapy/Pharmacy/Radiology, Laboratory Director; site/regional ops
managers, Administrators and single-facility Executive Directors;
practice/office/clinic managers and scheduling/staffing coordinators.

**The discipline-matching ladder is DELETED** (CMO→physician roles,
CNO→nursing, Director of Pharmacy→pharmacists, Director of Rehab→therapy). It
was measured and failed: chief-level clinical went 0 for 126 (CMO 0/78, CNO +
Director of Nursing 0/29, Chief Clinical Officer 0/19). Do not rebuild it.

Implemented in `apollo-dm-waterfall`'s `RANK_SYSTEM` **and** its
`LARGE_ORG_TITLES` Apollo search filter — the two must stay in sync, or the
ranker is handed a candidate page with nobody valid on it.

## Phase 4 — generation (`generate_healthcare_demand.py`)

Per lead: persona from DM title (A clinical / B owner / C top-HR), age band
from Date Published (30-60 pain opener / 8-29 before-it-drags / 0-7
shortcut-the-slog), GPT-4.1 extracts controlled variables (cleaned_role,
role_plural, employer_type org/people-form, team_word, casual_company,
duration_stat, in_scope), body assembled deterministically, then a
**GPT-5.1 reviewer agent** fixes only mechanical insertion errors (a/an,
mismatched employer type/team word) with guardrails that protect the claims.
- `in_scope` gate skips non-clinical roles (research scientists, front desk).
- Ambiguity rule: when employer type is not unmistakable → "healthcare
  employers". Pain word must match company type.
- Writes: Z body (NO sign-off — the sequence appends it), AD-AL audit trail
  (persona, age_band, cleaned_role, role_plural, team_word, employer_type,
  month, casual_company, review_status).

## Phase 5 — push (`push_healthcare_demand.py`)

- **`custom_variables` is the correct API field** — Instantly merges it into
  the stored lead payload. Nesting under `payload` on POST gets silently
  replaced; loose top-level keys are dropped. (445 leads once pushed without
  variables because of this.)
- Variable contract per lead: personalization, cleaned_role, role_plural,
  team_word, employer_type, month, casual_company, city, job_title, dm_title,
  job_post_url (`https://indeed.com/viewjob?jk={Job_Id}` — NOT the apply URL),
  company_website.
- Guards: dupes (in-run + AA=TRUE anywhere), email-root-domain vs website
  (mismatches HELD for review, not pushed — most are legit corporate-parent
  domains), blocklist 400s → AA=BLOCKLISTED permanent.

## Instantly campaign conventions (reference: f59f4fa5-ec7d-45b6-92b4-4ec6417e66cd)

- Draft status; Jude activates in the UI. Sending accounts only on Jude's
  explicit instruction (July 2026: "Prewarmed - Instantly" tagged mailboxes).
- 4 steps, delays 2/3/4/6 (day 0 / +3 / +7 / +13), in-thread (empty subjects
  on follow-ups), stop_on_reply, text_only + first_email_text_only, tracking
  off, daily_limit 500.
- Step 1 subject: `{{firstName}}, the {{cleaned_role}} hire`; body
  `{{personalization}}` + sign-off. Sign-off: `Best, {{sendingAccountFirstName}}`
  + blank line + `Sent from my iPhone` (persona mailboxes are not Jude).
- Follow-up arc: mechanism (bench of pre-vetted candidates) → light check-in
  (are you still hiring) → graceful breakup. Never bare bumps; re-anchor
  72-hour sourcing + 30-day refund + no upfront commitment.
- Funnel: email CTA = "Should I make the intro?"; positive reply → CC the
  recruiter on the thread; recruiter takes over. No price/leads before intro.
