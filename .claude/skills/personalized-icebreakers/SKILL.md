# personalized-icebreakers

Niche-agnostic personalized icebreaker generation from deep per-lead research:
the lead's LinkedIn profile plus a whole-site crawl of their company website,
synthesized into a dossier, then worded as a two-specific opener in Nick
Saraev's style: `Love {specific 1}. Also {specific 2}.`

Built July 2026 for the healthcare retarget campaign. Column letters are all CLI
flags, so it runs against any sheet schema. Website crawling is direct HTTP (no
Apify cost); LinkedIn uses the dev_fusion actor ($3 per 1,000 profiles). All LLM
calls are Azure OpenAI GPT-4.1 (`AZURE_OPENAI_DEPLOYMENT_FAST`).

## Why this exists

The alternative is a static icebreaker (see `recruitment-email-gen`), where every
lead gets the same line with only the first name swapped. That is honest but
generic.

The failure mode this skill is designed around: **agency websites are near-identical
marketing boilerplate.** Point an LLM at one and demand a specific compliment and
it produces "Love how you put people first in everything you do." That reads as
fake-personalized, which fires the recipient's template detector harder than an
honest generic line would. So the pipeline is built to return **nothing** when the
research has nothing concrete: an empty cell, no static fallback by default.
Jude's rule: no icebreaker beats a sloppy one.

The second design goal is that the line must pass as human-typed. The generator
prompt is principles-led (why generated text gets detected: symmetry, verb
monoculture, uniform polish, over-completeness, over-precision) and the batch
carries a rolling avoid-list so openers, perception verbs, connectors, and tail
phrases never repeat across nearby rows.

## Phase sequence

| Phase | Script | Purpose |
|-------|--------|---------|
| 0 | `scrape_linkedin.py` | Pull the DM's LinkedIn profile (compacted JSON) into one column |
| 1 | `scrape_facts.py` | Whole-site crawl, PER-PAGE abstracts as JSON in one column |
| 2 | `build_dossier.py` | Merge LinkedIn + website into a research dossier: summary, niche, healthcare fit, best/second fact with when-tags, flags |
| 3 | `generate_icebreaker.py` | n=3 generate → gate → verify-revise loop → the line (or empty) |
| 4 | `generate_body.py` | Assemble full email: greeting + icebreaker + Jude's fixed offer template, routed by healthcare_fit |

Separate phases so each stage is re-runnable alone: retune copy by re-running
phase 3 only, no re-scraping. Every script is batch-of-10, resume-safe (skips
filled output cells); blocked scrapes recover by simply re-running.

## Phase 0 - LinkedIn (`scrape_linkedin.py`)

Highest-yield source: a profile that scrapes always carries tenure, and `about`
holds founder stories and self-published numbers. Keeps name, title, headline,
about, company (+size/industry/website/linkedin/founded), location, tenure,
awards, certifications, experiences, educations. ~25-30% of profiles come back
blocked per pass; re-run to retry blanks.

## Phase 1 - website crawl (`scrape_facts.py`)

Crawls the WHOLE site (Jude's rule: not just a few guessed paths): homepage link
discovery, breadth-first, priority-scored (team/story pages first, then
news/press, about, services), full Chrome header set for WAF evasion, homepage
warm-up, early bail on dead sites. Bounds: 25 pages, ~14k chars, 75s per site.

Output is **per-page abstracts** (`{"pages":[{"url","abstract"}],"n":N}`), never
one concatenated blob: the homepage headline drowns buried details otherwise,
and buried details are the whole point. Boilerplate pages abstract to SKIP and
are dropped. ~35% of agency sites WAF-block even with good headers; LinkedIn is
the fallback source for those rows.

## Phase 2 - dossier (`build_dossier.py`)

Synthesizes both sources into: `research_summary` (120-220 words),
`primary_niche` (18-value vocabulary incl. `not_a_recruiter`/`unknown`),
`niche_detail`, `roles_placed`, `geography`, `healthcare_fit`
(primary/partial/none/unknown — this drives copy routing), `best_fact`,
`second_fact`, and **when-tags** for both facts
(`prior_career`/`current_company`/`dated_event`) so the generator never frames a
current fact as the past. Flags column gets `MOVED->{company}` when LinkedIn
shows the lead left the sheet's company, and `NOT_A_RECRUITER` (skipped by
phase 3). LinkedIn overrides the sheet as source of truth. Today's date is
injected for recency judgment.

Fact priority: prior career > published numbers > awards > milestone > narrow
specialism. NEVER education. Business model/structure and self-classification
(directory categories) are banned fact types.

## Phase 3 - generation (`generate_icebreaker.py`)

Per row: 3 candidates at temp 0.8 → mechanical gates → temp-0 reviewer →
verify-revise loop (reviewer feedback seeds up to 2 revision calls; blind
resampling never converges, feedback does). Never converged = empty cell.

**Mechanical gates** (`is_sane`/`is_specific`): length bounds, sentence-final
punctuation, "Noticed" opener ban, banned-substring list (values/culture praise,
difficulty framing, "impress", generic love), education backstop,
self-classification backstop, sender-fabrication backstop, "Also btw"/trailing
btw ban, recipient-name-in-third-person check, must contain a hard specific
(digit, interior capitalized token, or content-word overlap with the research).

**Reviewer severity tiers**: HARD checks (faithfulness incl. number scrutiny,
timeline vs when-tags, two independent specifics, no third-party assessment, no
education, dignity, no speculation/burden framing, no self-classification,
complete opener, btw placement, no sender fabrication) force rejection with
actionable feedback. STYLE checks (filler, generated-text tells, warmth, form)
are feedback-only and never zero a row. The reviewer may salvage a line by
trimming its one bad half. The judge runs even on a single surviving candidate
(unjudged singles leaked commentary halves).

**Style rotation**: opener shapes (love-that / love-how / unexpected) and
register hints (mostly clean, occasional "Btw, also ...") rotate by row; an
avoid-list built from the last 8 emitted lines suppresses repeated openers,
perception verbs, connectors, and tail phrases.

## Phase 4 - body assembly (`generate_body.py`)

The offer copy is **Jude's template verbatim**, stored at the top of the script
(swap the whole block per A/B test). The ONLY authorized variables are `{icp}`
and `{roles}` ("...tracking and actively engaging with {icp} hiring for {roles}
right now..."); the CTA, proof line, and sign-off are fixed and must never be
reworded. Routing by dossier `healthcare_fit`: primary/partial → "healthcare
employers" + "clinical staff" (slot-extracted roles for non-clinical healthcare
recruiters, e.g. healthcare IT); none → both slots extracted from their actual
niche ("law firms" / "attorneys"); unknown → generic. Slot extraction is one
GPT-4.1 call, validated (word caps, banned agency-words, no employer/roles stem
doubling, "it"→"IT") with deterministic fallbacks.

**Rows with no icebreaker are skipped entirely and never sent** — the campaign
is a personalization-only experiment (Jude's rule, July 2026). NOT_A_RECRUITER
rows are skipped too. Writes `--col_body` (full plain-text body, rides as
`{{personalization}}`) and `--col_variant` (healthcare / healthcare_partial /
niche / generic).

## Copy rules (Jude's, accumulated)

- **The v3 formula (locked July 2026): `Love {specific 1}, {compliment tied
  to specific 1}. Btw, also noticed/saw how/that {specific 2 as a clause}.`**
  The compliment bridges the halves (without it the two facts read like index
  cards); it must credit them and be safe if slightly wrong. The second half
  ALWAYS opens "Btw, also noticed" or "Btw, also saw" followed by a mandatory
  "how" or "that" (bare noun objects banned), and must add a NEW concrete
  detail not in the first half. Openers are Love-family ONLY (other shapes
  hallucinated reactions).
- Reads human: contractions, rounded numbers ("since 84", "20-odd years").
- Capitalization mimics fast typing: sentence starts and PEOPLE'S names
  capitalized; company/brand names and acronyms lowercase ("volthire",
  "shrm-cp"). Correct branding everywhere is an AI tell.
- Both specifics positive; never frame their niche/market as hard or slow.
- Never education, in either half.
- Mention colleagues as facts, never assess them.
- No speculation about them ("you must have some stories" — the word "must"
  is mechanically banned) and no burden framing ("a lot to juggle").
- Dignity: no menial/student jobs, no hardship (janitor pattern mechanically
  banned after the reviewer passed it twice).
- No surveillance material: posted pay rates, assignment listings, registered
  entity names (Ltd/LLC/Inc), HQ locations, travel-tips page content. Filtered
  mechanically at fact-input AND line-output level (`is_surveillance`).
- Never invent anything about the sender (location, familiarity, prior contact).
- No em dashes anywhere. NO word limit (Jude removed it; 60-word runaway
  guard only). Empty beats filler.

## Commands

Column letters below are the Healthcare Retarget sheet's layout; adjust per sheet.

```bash
SKILL=.claude/skills/personalized-icebreakers/scripts

# Phase 0 - LinkedIn profiles (re-run to retry blocked rows)
python3 -W ignore $SKILL/scrape_linkedin.py --sheet_url "URL" --tab "TAB" \
  --col_linkedin O --col_out Q [--limit N]

# Phase 1 - whole-site crawl (test on a small batch first)
python3 -W ignore $SKILL/scrape_facts.py --sheet_url "URL" --tab "TAB" \
  --col_website L --col_out K [--limit N] [--preview N]

# Phase 2 - dossiers
python3 -W ignore $SKILL/build_dossier.py --sheet_url "URL" --tab "TAB" \
  [--limit N] [--preview N]

# Phase 3 - preview REQUIRED before the real run (email-gen approval gate)
python3 -W ignore $SKILL/generate_icebreaker.py --sheet_url "URL" --tab "TAB" \
  --col_facts K --col_icebreaker L --col_fact_type M \
  --col_first B --col_last C --col_title G --col_company D \
  --col_best_fact V --col_summary R --col_second_fact Y \
  --col_best_when Z --col_second_when AA --col_flags X --preview 20

# Phase 4 - full body assembly (icebreaker required; preview gate applies)
python3 -W ignore $SKILL/generate_body.py --sheet_url "URL" --tab "TAB" \
  --col_first B --col_icebreaker L --col_niche S --col_niche_detail T \
  --col_hc_fit U --col_roles W --col_flags X --col_summary R \
  --col_body AB --col_variant AC --preview 15
```

**`--limit` counts PENDING rows, not sheet rows** — on a partially processed
sheet a `--limit 20` run lands wherever the next 20 unfilled rows are.

## Rules inherited from the repo

- **Batch-of-10 writes**, resume-safe, skip already-populated cells.
- **No em dashes.** Scrubbed mechanically on output.
- **Preview before write.** Phase 3 output must be shown and approved before a
  real run, same gate as any email generation step.
- **MOVED-flagged rows**: the sheet email may be dead; re-verify before any push.
