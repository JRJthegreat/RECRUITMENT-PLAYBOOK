# apollo-dm-waterfall

Niche-agnostic decision-maker discovery + verified-email waterfall. Finds the
right DM at a company (by domain) and their verified email for **~1 AMF credit
per company and 0 Apollo credits** — replaces AnyMail Finder's /decision-maker
endpoint (2 credits, no title/LinkedIn) with better targeting.

Built July 2026 for the healthcare demand pipeline; reusable for HR/tech/any
niche by editing `RANK_SYSTEM` in `apollo_dm_waterfall.py` (the ranking rules
are the only niche-specific part). Column letters are all CLI flags, so it
runs against any sheet schema.

---

## How the waterfall works (per company row)

1. **Apollo People Search** (`POST /api/v1/mixed_people/api_search`, FREE) by
   company domain → candidate people. Large orgs are searched WITH a
   `person_titles` filter (`LARGE_ORG_TITLES`) so the 100-person page contains
   relevant leaders instead of random staff.
2. **GPT-4.1 ranks** up to 3 candidates by who owns the budget decision
   (rules in `RANK_SYSTEM`; receives org size band, open role, job location).
3. Per candidate until one verified email:
   a. **Google de-obfuscation** (apify~google-search-scraper):
      `{first} {company} {title} site:linkedin.com/in` → full last name
      (validated against the obfuscation pattern) + LinkedIn URL.
   b. **AMF /find-email/person** with full name + domain (1 credit ONLY on a
      found verified email; misses free).
   c. Google failed → **AMF with "{first} {last-initial}"** (catches
      first-name email formats like steli@close.io).
4. **Sheet-CEO rescue** (TINY/MID orgs only, never LARGE): if the sheet has a
   CEO name column (Indeed provides one), try it directly through AMF.
5. Writes DM Name/Title/LinkedIn/Email/First/Last + a status column
   (`found_p1_google+amf`, `no_apollo_people`, `not_found`, ...).
   **Never writes DM data without a valid email** — except under
   `--skip_email` (see below), the one sanctioned partial-row mode.
   Batch-of-10 writes, idempotent (rows with email or status are skipped on
   rerun).

## Run

```bash
python3 -W ignore .claude/skills/apollo-dm-waterfall/scripts/apollo_dm_waterfall.py \
  --sheet_url "URL" --limit 500
# Column flags (letter names, defaults match the 29-col Indeed schema):
#   --col_company K --col_website L --col_size M --col_city R --col_state S
#   --col_ceo O --col_job B --col_dm_name T --col_dm_title U --col_linkedin V
#   --col_email W --col_first X --col_last Y --col_status AB
# --dry_run shows the queue with size bands.
#
# Row ORDER (2026-07-31): the --limit budget is spent small-first by default
# — TINY/unknown, then MID, then LARGE, stable inside each band. Measured
# reply rate by size: TINY <50 3.52%, unknown 2.74%, MID 50-499 1.20%,
# LARGE 500+ 0.00% (0/244). Sorting never drops a row, it only decides who
# gets the credits when --limit bites.
#   --no_size_priority   plain sheet order (the pre-2026-07-31 behaviour)
#   --skip_large         drop 500+ entirely rather than deprioritise them.
#   --skip_email         IDENTITY ONLY — see below.
```

## `--skip_email` (identity only, 0 AMF credits)

Writes DM Name / Title / LinkedIn / First / Last and **stops before AnyMail
Finder**. Apollo search and the Google de-obfuscation step already produce a
real full name, title and LinkedIn URL before any AMF call, so this costs
nothing in AMF credits (Apollo search is free; Google-via-Apify is the only
spend).

- Rows are marked **`dm_only_p1` / `dm_only_p2` / `dm_only_p3`** in the status
  column, with the Email cell left blank.
- It requires a real de-obfuscated surname. If Google cannot resolve the name
  it moves to the next candidate rather than writing "Jeff W." — a downstream
  email tool needs a full name to work with.
- The sheet-CEO AMF rescue is skipped entirely in this mode.
- ⚠️ **This deliberately breaks the pipeline's "never write DM data without a
  valid email" rule.** It is the one sanctioned way to produce partial rows.

**Handoff contract for a later email pass:** target rows where the status
column starts with `dm_only_` and the Email cell is empty. Note the waterfall's
own resume logic skips any row with a non-empty status, so re-running
`apollo_dm_waterfall.py` without `--skip_email` will NOT pick these rows up —
the email step has to select them by status itself.

⚠️ **When running `--skip_email`, do NOT run the 2.9 / 2.9b AMF phases.**
`amf_ceo_rescue.py` (status `no_apollo_people`) and `amf_dm_fallback.py`
(status `not_found`) buy emails at 2 credits each. They will not touch
`dm_only_*` rows, but they will still spend on rows where the waterfall found
nobody. The "always run amf_dm_fallback" standing rule is suspended for any
identity-only batch.

## Size bands (drive the targeting rules)

- **TINY** (<50 employees or size unknown), **MID** (50-499), **LARGE** (500+),
  from the size column's lower bound.
- **Free size signal (default, 0 credits):** when the size cell is blank,
  Apollo's `total_entries` — people indexed at that domain, already in the
  free search response — is **written back to the size column** and upgrades
  the band: >=300 → LARGE (re-searched with the title filter), >=60 → MID.
  Blank-size rows therefore stop being permanent unknowns at no cost.
  ⚠️ It is an INDEXED-PEOPLE count, not true headcount. It undercounts, and
  undercounts worst at small practices where few staff have public profiles.
  For a 500 cap the error direction is the safe one: it may let a big org
  through, it will not wrongly drop a small one. Do not quote it to a client
  as a headcount.
- **Paid alternative, only if a real headcount is needed:**
  `apollo_org_enrich.py` (GET /organizations/enrich, ~1 Apollo credit per
  company, returns true headcount + HQ state → cols M + AM). Not part of the
  default flow — Jude's standing preference is the free signal above.
  (AC is reserved for the Indeed URL on every sheet — Jude's rule.)

## Rescue scripts

- `amf_ceo_rescue.py --sheet_url URL` — AMF /decision-maker, category `ceo`,
  for TINY rows with status `no_apollo_people`. 2 credits per found email.
  AMF has NO clinical/custom categories, so this is CEO-and-small-orgs only.
- `amf_dm_fallback.py --sheet_url URL` — AMF /decision-maker for `not_found`
  rows (waterfall ranked a DM but no verified email). Category `ceo` at EVERY
  band since 2026-07-31 (the old LARGE→`hr` routing is retired: 0 replies from
  32 HR contacts). 2 credits per found.
  **Standing rule (Jude): always run after the waterfall + CEO rescue —
  SUSPENDED for `--skip_email` batches, where no emails are wanted yet.**
- `apollo_org_enrich.py --sheet_url URL` — optional paid size enrichment (above).

## Hard-won API facts (verified live, July 2026)

- **Apollo Basic ($59/mo monthly)** has real API access. Old path
  `mixed_people/search` returns 403 — use `mixed_people/api_search`.
  Auth: `x-api-key` header.
- Search response per person is ONLY: `id`, `first_name`,
  `last_name_obfuscated`, `title`, `has_email`/`has_*` booleans,
  `organization.name`. **NO email, NO LinkedIn, NO last name, NO location.**
- **Obfuscation format:** first two letters + exactly three literal asterisks +
  last letter (`Wolfe` → `Wo***e`). Asterisk count does NOT encode length.
  Validator: candidate surname startswith(prefix) and endswith(suffix).
- People Search is the ONLY free Apollo endpoint. Organization Search is
  charged per page; all enrichment charged per record.
- **AMF only charges on found verified emails** — cascading through 3 people
  costs at most 1 credit. Person endpoint follows domain aliases
  (close.com → close.io).
- **amf_initial wrong-person risk:** initial-only lookups can return a
  different same-first-name person's mailbox (~29% of that method's finds in
  the first campaign). Validation heuristic: strip first name/initial from the
  email local part; the remainder must start with the stored last-initial.
  Jude's ruling: such leads are still usable (right company, right first name)
  but their copy must not assert the DM's exact title.
- Apollo company associations are noisy (contractors/ex-employees appear);
  Phase 2.5 LinkedIn verification (`verify_dms.py` in the Indeed pipelines)
  catches wrong-company and wrong-state people — but it over-flags
  commute-distance location differences, so review its flags, never blind
  `--apply`.

## Division of labor (Jude's standing rule)

**Apollo = DM identification ONLY (free search). AMF = ALL emails.**
Never use Apollo `people/match` for emails.
