---
name: production-directory-leads
description: Production-house supply pipeline sourced from the ProductionHub directory (Saad's brief's "main directory" for the commercial video production lane). Fires the custom Apify actor jude22/productionhub-directory-scraper over category × metro listing pages into a SQLite store, classifies out freelancers / wedding shooters / agencies using the profiles' self-written descriptions, enriches ICP survivors with full profile data (website, phone, LinkedIn), and exports shuffled 300-row campaign batches as Google Sheets in the repo's 29-col base layout. Use when the user asks to scrape ProductionHub, build the production directory list, or export a directory campaign batch.
---

# production-directory-leads

Supply side of the **commercial video production lane**, sourced from
**ProductionHub** — the source Saad's brief names as "the main directory for
commercial production companies". Replaces Google Maps as the volume source
for this vertical (the `production-house-leads` Maps store is parked as a
secondary pool; its 1,134 classified PRODUCTION_HOUSE rows remain usable).

## Architecture

Mirrors `production-house-leads` / `nppes-new-clinics`: **SQLite is the
database** (`data/directory.db`, gitignored), sheets are campaign batches cut
from it. `exported_at`/`batch_id` stamps guarantee a company is never worked
twice — and `export_batch.py` also checks the Maps store's exported domains,
so the two lanes never email the same company.

The scraper itself is a **custom Apify actor** (Python + patchright
Playwright), source at
`/Users/air/AIOS - AI OPERATING SYSTEMS/productionhub-directory-actor/`
(NOT in this repo), deployed as `jude22/productionhub-directory-scraper`
(actor id `IeRTbhbdczWrFJvOJ`, in config). ProductionHub is Cloudflare-
protected: the actor uses patchright + persistent context + headed Chromium
under xvfb — plain Playwright/headless never clears the challenge; don't
"simplify" that away.

## Phases

| Phase | Script | Purpose |
|-------|--------|---------|
| 1 | `scrape_directory.py` | Fire actor over category × metro listing pages (LISTING-ONLY — no profile visits) → upsert into store keyed by profile_id. `--dry_run` scope preview; `--dataset_id X` ingests a pre-existing run |
| 2 | `classify_directory.py` | GPT-4.1 (Azure fast) over name + categories + self-written description → same 9-class vocabulary as the Maps lane. Leans UNCERTAIN; nothing deleted |
| 2.5 | `enrich_profiles.py` | Actor in visit-only mode (`profileIds`) for PRODUCTION_HOUSE rows: full description, website, phone, LinkedIn, Vimeo. `--classes UNCERTAIN` = refinement pass, then `classify_directory.py --retry_uncertain` |
| 3 | `export_batch.py` | Shuffled 300-row batch of unexported PRODUCTION_HOUSE rows with domains → new Google Sheet, 29-col base layout, stamps store |

After export the standard tail applies: `exa-website-enrichment` for rows
still missing domains (company/website/city/state/status sit at K/L/R/S/AB —
its default flags), then `apollo-dm-waterfall` → AMF → template approval →
Instantly push.

## Why listing-first, enrich-later

Listing pages carry ~55 profiles per page-load (name, city, description
snippet, member-since) — enough for classification. Profile pages cost one
challenge-able page-load EACH and are only needed for website/phone/LinkedIn.
So Phase 1 sweeps listings fast, Phase 2 judges everyone, and Phase 2.5
spends the slow visits on ICP survivors only. Same judge-first-spend-second
discipline as the DM rules.

## Commands

```bash
# scope preview
python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py --dry_run

# first-run verification: one metro × one category
python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py \
    --metros LA --categories commercial-production-companies

# full sweep (all metros × all 4 categories)
python3 -W ignore .claude/skills/production-directory-leads/scripts/scrape_directory.py

# classify (resume-safe)
python3 -W ignore .claude/skills/production-directory-leads/scripts/classify_directory.py --limit 40
python3 -W ignore .claude/skills/production-directory-leads/scripts/classify_directory.py

# enrich ICP survivors (batches — the visits are slow, ~5s each)
python3 -W ignore .claude/skills/production-directory-leads/scripts/enrich_profiles.py --limit 300

# UNCERTAIN refinement loop
python3 -W ignore .claude/skills/production-directory-leads/scripts/enrich_profiles.py --classes UNCERTAIN --limit 300
python3 -W ignore .claude/skills/production-directory-leads/scripts/classify_directory.py --retry_uncertain

# export a campaign batch
python3 -W ignore .claude/skills/production-directory-leads/scripts/export_batch.py --dry_run
python3 -W ignore .claude/skills/production-directory-leads/scripts/export_batch.py
```

## Quirks & gotchas

- **Geography (verified 2026-08-10):** ProductionHub is US + Canada only.
  `/uk/england/london` resolves but has zero listings — London, Amsterdam and
  Berlin need LBBonline or a curated pass (not built). Config metros: LA,
  NYC, Austin, Nashville, Chicago, Miami, Atlanta, Toronto.
- **Websites are NOT a directory field.** They only exist where the company
  wrote a URL into its own blurb (~60% of visited profiles on the test
  sample) or linked it. `domain IS NULL` after enrichment is normal — finish
  those rows with `exa-website-enrichment`.
- **Freelancers are mixed into every category** ("I'm a Director/DP…") — the
  classifier's FREELANCER class leans on first-person voice. Expect a lower
  junk rate than Maps but not zero.
- **Radius overlap:** each metro is scraped with ProductionHub's default
  100-mile radius, so LA pulls San Diego and NYC pulls Philadelphia-ish rows.
  Upsert-by-profile_id makes the overlap harmless; `metro` records whichever
  target found the profile first.
- **The actor's `views` field** feeds export dedupe (highest views wins per
  domain). `member_since` maps to the brief's "founded 3-15 years ago" filter
  loosely — it's directory-join year, not founding year; don't filter on it
  mechanically.
- **Actor input modes are mutually exclusive:** `targets` = listing crawl,
  `profileIds` = visit-only. Passing both runs visit-only.
- **Store config changes don't self-correct stored rows** (same as nppes):
  the upsert only fills blanks. There is no resync script yet — if
  normalization rules change, write one before re-pulling.
