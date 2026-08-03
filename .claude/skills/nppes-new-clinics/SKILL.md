---
name: nppes-new-clinics
description: Detect newly-registered medical practices (new organization NPIs) in target states from CMS NPPES weekly files, classify out reorganizations and solo-PLLC shells, score, and export a contactable list — BEFORE the practice posts its first job ad. Use when the user asks to find new/about-to-open clinics, run the NPPES pipeline, pull new practice registrations, or source pre-job-posting healthcare demand.
---

# nppes-new-clinics

Implements Jude's **NPPES New-Practice Detection Pipeline spec** (Google Doc
`1VZMcgLAy8IKGb5r7rvrto40-Gs2UxF0hIO-vRY3GNrQ`). Every other operator sources
healthcare hiring demand from Indeed/LinkedIn and finds the same flooded
practices at the same time. A practice that just registered an organization
NPI has NOT posted a job yet — for insurance-accepting clinics the NPI lands
3-8 months before opening (payer credentialing forces it early) and roughly
1-3 months before staff job ads. This pipeline finds those practices weekly
from public CMS data, free.

**v1 scope:** NPPES only → scored CSV/Sheet. Outreach and enrichment stay in
the existing stack. States: ALL US — 50 states + DC (Jude widened from the
spec's IN/IL/TX/CA on 2026-07-23; per-state volume was too thin). Territories,
military codes, and foreign addresses are excluded. Window: 90 days. Both in
`config/settings.json` (`"states": "ALL"` or a list).

## Phases

| Phase | Script | Cost | Purpose |
|-------|--------|------|---------|
| 1 | `pull_new_practices.py` | free | Weekly V2 files → filter (org + window + state + taxonomy) → SQLite |
| 1.5 | `build_baseline.py` | free, ~1.1GB download | Monthly full file → address/name novelty baseline (rebuild monthly) |
| 2 | `classify_practices.py` | free | NEW_INDEPENDENT / HEALTH_SYSTEM_EXPANSION / NEW_LOCATION / LIKELY_ADMIN / UNCERTAIN + solo-PLLC flag + score |

**Classifications.** `NEW_INDEPENDENT` = no parent/subpart, novel address, no
name match. **`HEALTH_SYSTEM_EXPANSION`** = system subpart or named parent at
a NOVEL address — a system opening a new site; HIGH VALUE (budget, repeat
buyer, one relationship spans every future site they open). **The spec's §4.1
"subpart → deprioritize or drop" rule is deliberately OVERRIDDEN** (Jude,
2026-07-23): §4.1 optimizes for "is this a new *business*", but the business
question is "is this a new *site* that needs staff". A subpart at an address
the parent already operates is still `LIKELY_ADMIN` (billing reorg).
`NEW_LOCATION` = existing brand at a new address. `UNCERTAIN` = no baseline.
| 3 | `export_leads.py` | free | Scored CSV (spec §8 columns) + optional Google Sheet |
| util | `resync_store.py` | free | Re-apply current allowlist/normalization to stored rows after config tuning |

```bash
python3 -W ignore .claude/skills/nppes-new-clinics/scripts/pull_new_practices.py
python3 -W ignore .claude/skills/nppes-new-clinics/scripts/build_baseline.py          # monthly
python3 -W ignore .claude/skills/nppes-new-clinics/scripts/classify_practices.py
python3 -W ignore .claude/skills/nppes-new-clinics/scripts/export_leads.py --to_sheet
```

All scripts support `--dry_run`; pull/classify/export support `--limit N`.
Weekly cadence: run pull → classify → export every Monday after ~07:30 UTC
(CMS posts the new weekly file Monday morning). Rebuild the baseline after
the monthly release (second Monday).

## Config (never hardcode)

- `config/settings.json` — states, window_days, scoring weights, name-match
  threshold, owner-title keywords.
- `config/taxonomy_allowlist.json` — **VOLUME-FIRST since 2026-07-23** (Jude:
  "any medical clinic is a viable target. I need volume. let the data
  decide" — supersedes the spec's narrow §3.4 list). Broad include +
  `_denylist`: every medical care org gets in; only non-clinics are dropped
  (DME, transport, labs, pharmacies, payers, social-services, gov/military,
  religious-nonmedical). Named categories label known segments (order
  matters — precise codes first); everything else lands in the
  `other_medical` catch-all. Qualification happens DOWNSTREAM via
  classification + solo flag + score, and the pitch is generic connector
  copy ("recruiters taking on newly open clinics"), so specialty never
  drives the send.

## Data source facts (verified live 2026-07-23)

- Weekly V2: `https://download.cms.gov/nppes/NPPES_Data_Dissemination_MMDDYY_MMDDYY_Weekly_V2.zip`
  (Monday+Sunday dates, posts following Monday ~07:15 UTC, 6-17MB). Old URLs
  stay live ≥12 months even though the index page only lists ~2. **V1 (non-_V2)
  files are dead** — build on V2 only.
- Monthly full V2: `NPPES_Data_Dissemination_{Month}_{YYYY}_V2.zip`, ~1.07GB
  zip / ~9GB CSV, second Monday. Used ONLY for the baseline; streamed from the
  zip, never extracted.
- Weekly files mix new enumerations with updates + deactivation stubs:
  **filter on Provider Enumeration Date, never on file presence**. Stub rows
  have a blank Entity Type Code — drop before anything else. The first weekly
  after a monthly release is ~4x normal size (update bloat, not new orgs).
- ~2,600-2,700 genuinely new NPI-2/week nationally. New-org rows have 100%
  fill on Authorized Official name/title/phone and Taxonomy_1. DBAs live in
  `othername_pfile` inside the same zip (~12% of new orgs), NOT in the main
  file's Other Name column.
- Primary taxonomy convention: switch=='Y' wins; no Y → Code_1. (Switch can
  be Y/N/X.)
- **The NPPES API cannot do this job**: no enumeration-date filter (params
  silently ignored), limit ≤200, skip silently clamped at 1000 (max 1,200
  rows/query, silent duplicate pages past the cap). API is for per-NPI
  lookups only.

## Contamination doctrine (why the classify phase exists)

- ~45-50% of new NPI-2s are solo-clinician PLLCs (a counselor/NP/dentist
  incorporating their own billing entity) — already-practicing people, not
  staffing launches. The deterministic solo flag (AO surname inside org name,
  or clinical credential inside a short name) catches most; it's a score
  penalty, not a drop — some person-named entities are real group launches.
- Subparts, parent-org records, and known addresses are reorganizations of
  existing organizations → LIKELY_ADMIN (score −5). Beyond the ~6% that
  self-flag as subparts, an unquantified share of "new" NPIs are ownership
  changes (CHOWs) that look identical — the address-novelty check is the main
  defense, which is why the baseline matters.
- `enumeration_date` ≈ registered, not opened. Say "registered", never
  "opened". Cash-pay practices (med spa pattern) register only ~2-6 weeks
  out; insurance practices 3-8 months.
- Practice address for a pre-opening clinic is often the owner's HOME —
  don't trust address-based personalization.

## Honest volume expectations (measured July 2026)

Allowlisted new practices per 90 days, measured live: CA 1,142, TX 965,
IL 440, IN 149 — i.e. even TX+IL+IN together is only ~120/week raw, which is
why scope went national. National: ~950/week raw allowlisted (~12k/90d),
roughly 400-550/week surviving classification. Single states are thin
(IN ~11/week raw) — this pipeline is a national play that gets FILTERED to a
client's geography at export time (`export_leads.py --states IN,TX`), never
scraped per-state.

## Validation checkpoint (spec §11 step 4 — DO NOT SKIP)

Before any enrichment spend or outreach: pull 20-30 NEW_INDEPENDENT records,
manually verify they look like genuinely new practices (search the org name,
check for an existing site/jobs), and confirm with Jude. If classification is
wrong, everything downstream is polished noise.

## Out of scope in v1 (deliberate)

- **Enrichment**: NPPES has phone + named authorized official but NO email/
  domain. When Jude green-lights enrichment: domain resolution via
  `find_company_domains.py` pattern (add city/state to the query — clinic
  names collide), then AMF person endpoint with the AO name (the AO usually
  IS the owner at small practices), then `apollo-dm-waterfall` as rescue.
  Practice/office-manager AO titles are BANNED as email targets per playbook.
- **Outreach**: expect a large `phone_first` share — a brand-new practice
  often has no domain yet. That's routing info, not failure.
- State license boards, CON filings, SoS registrations, permits.
- Automated sends. Phase 4/5 equivalents live in the existing stack with the
  usual template-approval and manual-stop gates.

## Store

`data/nppes.db` (SQLite WAL, gitignored): `practices` (one row per NPI ever
pulled — the dedupe layer), `baseline_addresses` / `baseline_orgs` (novelty
reference, includes DBA/trade names), `meta`, `runs`. Raw zips cache in
`data/raw/`; exports land in `data/output/`.

Idempotency contract:
- Pull skips known NPIs → **config changes never self-correct stored rows;
  run `resync_store.py` after tuning the allowlist or normalization.** Rows
  falling off the allowlist become `OFF_TAXONOMY` (kept, never deleted,
  excluded from export).
- **Exports are deltas by default** (`exported_at IS NULL`); pass
  `--include_exported` for a full re-export. `--mark_contacted npis.txt`
  stamps worked leads so they never re-surface.
- `addr_is_novel` values: 1 novel, 0 an org already at the address
  (→ LIKELY_ADMIN), 2 only individual providers at the address (often the
  founder's own Type-1 NPI — treated as novel, kept distinct for review).
- Known blind spot (accepted for v1): a CHOW where the seller's NPI was
  deactivated before the monthly snapshot classifies NEW_INDEPENDENT —
  deactivation stubs carry no address, so the baseline can't see the old
  tenant. The §11 validation checkpoint is the control for this.
