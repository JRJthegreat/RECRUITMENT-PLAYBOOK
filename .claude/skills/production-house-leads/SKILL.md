---
name: production-house-leads
description: Production-house supply pipeline for the commercial video production lane. Scrapes video/commercial production companies from Google Maps across a metro grid (LA, NYC, Toronto, London, Amsterdam + secondary hubs) into a SQLite store, classifies out wedding videographers / photo studios / freelancers / agencies, and exports shuffled 300-row campaign batches as Google Sheets in the repo's 29-col base layout for DM + email enrichment and Instantly push. Use when the user asks to scrape production houses, build the production supply list, or export a production campaign batch.
---

# production-house-leads

Supply side of the **commercial video production lane** (new ICP, Aug 2026).
NEXAM connects production houses (the paying client) with companies that need
video produced. This skill builds and feeds the supply list.

Source brief: "Rood's Playbook" from Saad (Google Doc, 2026-07-31), section 2.

## Architecture

Mirrors `nppes-new-clinics`, not the sheet-first pipelines: **SQLite is the
database** (`data/production.db`, gitignored), sheets are campaign batches cut
from it. Scrape everything once, then export deltas of ~300 as campaigns need
feeding — `exported_at`/`batch_id` stamps guarantee a company is never worked
twice.

## ICP filters (from the brief)

- 5-40 employees (below 5 = one-man band, above 40 = has own BD team)
- Founded 3-15 years ago
- Active: recent work posted in last 90 days
- ≥2 recognizable brands on the client list
- Not locked into agency-of-record contracts

Size/founding/activity are NOT filterable from Google Maps metadata — the
store captures everything and the filters are applied downstream:
size via `apollo_org_enrich.py` after export, activity/client-list signals
are visible to the icebreaker/personalization pass if one runs.

## Phases

| Phase | Script | Purpose |
|-------|--------|---------|
| 1 | `scrape_maps.py` | Apify `compass/crawler-google-places` per metro × 5 search terms → upsert into store keyed by place_id. `--dry_run` prints scope + upper-bound cost. `--dataset_id X --metro_key Y` ingests a pre-existing run |
| 2 | `classify_studios.py` | GPT-4.1 (Azure fast) over Maps metadata → PRODUCTION_HOUSE / POST_ONLY / FREELANCER / WEDDING_EVENT / PHOTO_ONLY / EQUIPMENT_STUDIO / AGENCY / MEDIA_OTHER / UNCERTAIN. Leans UNCERTAIN; nothing is deleted, wrong classes just never export |
| 3 | `export_batch.py` | Shuffled batch (default 300) of unexported PRODUCTION_HOUSE rows with a real domain → new Google Sheet, 29-col base layout, `exported_at` + `batch_id` stamped |

```bash
# Phase 1 — always dry-run first, the cost ceiling prints there
python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py --dry_run
python3 -W ignore .claude/skills/production-house-leads/scripts/scrape_maps.py --metros LA,NYC

# Phase 2
python3 -W ignore .claude/skills/production-house-leads/scripts/classify_studios.py --limit 100

# Phase 3
python3 -W ignore .claude/skills/production-house-leads/scripts/export_batch.py --dry_run
python3 -W ignore .claude/skills/production-house-leads/scripts/export_batch.py
```

## Downstream (existing skills, batch sheet as input)

The export layout deliberately puts company/website/city/state at K/L/R/S and
the DM block at T-W so these run with default flags:

1. **Size**: `apollo-dm-waterfall/scripts/apollo_org_enrich.py` → headcount in
   col M. Then the 5-40 band from the brief is applied by dropping/skipping
   out-of-band rows at DM time (do not delete rows — mark AB).
2. **DM discovery**: `apollo-dm-waterfall` — target ladder for this lane:
   **Owner / Founder / CEO → Executive Producer → Managing Director/Partner**.
   `RANK_SYSTEM` in `apollo_dm_waterfall.py` is niche-specific and currently
   healthcare-tuned; it must be retargeted for production before running
   (check with Jude whether to flag-switch or clone).
3. **Emails**: inside the waterfall (AMF person, valid-only, domain-match) +
   the standing `amf_dm_fallback.py` pass.
4. **Copy + push**: per-campaign clone scripts (repo convention). Saad's copy
   framework is the base, adapted to standing rules: no sign-off in bodies,
   no em dashes, casualization, text-only, DRAFT campaign. Approval gate
   applies before any generation run.

## Metro grid

Config-driven (`config/settings.json`): LA (6 sub-queries), NYC (4),
Austin, Nashville, Chicago, Miami, Atlanta, Toronto, London, Amsterdam,
Berlin. Search terms: video production company, commercial video production,
film production company, video production studio, corporate video production.

## Quirks

- **Social-only websites**: many small studios list Vimeo/Instagram as their
  Maps website. The URL is kept in `website` but `domain` stays NULL
  (`SOCIAL_HOSTS` in `production_common.py`) — those rows are held out of
  export until a domain-resolution pass (`exa-website-enrichment`) fills them.
- **Domain dedupe at export, not ingest**: multi-office studios appear once
  per office in Maps; the store keeps all offices, export takes one row per
  domain (highest review count wins).
- **Google Maps runs are slow** (5 terms × up to 200 places per location
  query): run Phase 1 in the background. A run that fails mid-way prints the
  `--dataset_id` recovery command.
- Classification is metadata-only by design. UNCERTAIN rows are a future
  website-scrape refinement pass, not junk.
