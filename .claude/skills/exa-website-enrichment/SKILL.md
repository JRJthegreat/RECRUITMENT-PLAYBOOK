---
name: exa-website-enrichment
description: Resolve a company's official website domain via Exa and write it ONLY when the page itself proves the match. Niche-agnostic, column letters are CLI flags, so it runs against any sheet. Rejects directories, job boards, careers subdomains and parent-chain domains outright, and leaves a cell blank rather than writing a domain it cannot prove. Use when a lead sheet has missing or untrustworthy company websites, before any DM or email enrichment.
---

# exa-website-enrichment

## What this is for

Every downstream step in this repo keys off the company domain — Apollo searches
by it, AnyMail Finder verifies against it. A wrong domain does not fail loudly;
it quietly finds a real person at the **wrong company** and emails them. So the
job of this skill is not "fill column L". It is **never write a domain we cannot
prove**, and leave the rest blank for a human.

Modelled on Jude's `roofingskill` Exa resolver, with one rule deliberately
changed — see below.

## Why the two cheaper approaches were rejected

Both were measured on the Florida healthcare sheet (1,109 companies, Aug 2026).

**Guessing the domain from the company name**, then checking the page mentions
the name: **25% recall, ~50% precision**. It is circular — a page at
`lakenona.com` mentions "Lake Nona" whether it is the clinic or the property
developer. Real output: `orlando.org` (Orlando Economic Partnership) for Orlando
Health, `lhc.org` (a church in Austin, TX) for The Lakes Home Care,
`precision.com` (a printer-supplies firm) for Precision Healthcare Specialists.

**Google-via-Apify + an LLM picking a result**: better, and fine for bulk, but it
silently returned **parent-chain and careers domains** — 28 HCA facilities all
resolved to `careers.hcahealthcare.com` and 15 Life Care homes to `lcca.com`.
Apollo then hands back the same corporate person for all 28, and you email one
human 28 times.

Both failures share a cause: the domain was accepted because of how it *looked*,
not because of what was *on the page*.

## The one rule changed from roofingskill

`roofingskill` requires the identity prefix to appear IN the domain
(`Oak Valley Roofing` → `oakvalleyroofing.com`) and rejects everything else.
That is correct for roofing and **wrong for healthcare**, where practices trade
under sister brands and service+city domains. Two verified-correct matches from
Jude's own sheet that the prefix rule would have thrown away:

| Company | Domain | Why the prefix rule fails |
|---|---|---|
| Easy Reach PT Rehab | easyreachchiro.com | sister brand (chiro, not PT) |
| Pamela Rowe Speech Therapy | speechorlando.com | service + city, no name at all |

## How a domain gets accepted (rebuilt 2026-08-10, Jude's calls)

The old mechanical ladder (prefix-in-domain fast path + token/location content
match) is **gone** — it false-positived whenever the domain didn't literally
resemble the name, and vice versa. The judgment structure now mirrors
`healthcare-demand-pipeline/scripts/find_company_domains.py` (Jude's tested
resolver feeding the LIVE Indiana pipeline), and **the judge is Claude in the
Claude Code session — no GPT, no per-call LLM API spend** (Jude, 2026-08-10).

Three steps:
1. **Collect** (`--apply`): one Exa search per row → mechanical prefilter →
   `exa_candidates.json` (domain + title + 300 chars of page text each).
   Rows with zero surviving candidates are stamped `exa_no_match` now.
2. **Judge** (Claude, in-session): read the candidates file, pick the official
   site per row or "". Sister brands, acronyms and service+city domains are
   allowed when the page text names the company; name collisions and
   wrong-location namesakes get "". Write `verdicts.json`.
3. **Apply** (`--verdicts verdicts.json --apply`): each verdict must be one of
   that row's collected candidates (else refused), parent-chain guard
   enforced, website + status written. No Exa spend in this step.

| Status | Meaning | Written |
|---|---|---|
| `ok_claude` | Claude picked it from the pre-filtered candidates | ✅ |
| `no_match` | no candidates survived, or Claude declined | ❌ |
| `verdict_refused` | verdict wasn't among the row's candidates | ❌ |
| `error:*` | Exa/HTTP failure — **not** the same as "no website" | ❌ |

## Run

```bash
# always dry-run first: prints the cost estimate, spends nothing
python3 -W ignore .claude/skills/exa-website-enrichment/scripts/enrich_websites_exa.py \
  --sheet_url "URL" --limit 20

# step 1 — collect candidates (the only step that spends Exa credits)
python3 -W ignore .claude/skills/exa-website-enrichment/scripts/enrich_websites_exa.py \
  --sheet_url "URL" --apply --niche "video production company" --candidates exa_candidates.json

# step 2 — Claude judges exa_candidates.json in-session, writes verdicts.json

# step 3 — apply verdicts (free)
python3 -W ignore .claude/skills/exa-website-enrichment/scripts/enrich_websites_exa.py \
  --sheet_url "URL" --candidates exa_candidates.json --verdicts verdicts.json --apply
```

| Flag | Meaning |
|---|---|
| `--col_company K --col_website L --col_city R --col_state S` | 29-col schema defaults |
| `--col_status AB` | writes `exa_<status>` per row; omit to skip |
| `--niche "healthcare practice"` | extra query words; sharpens results a lot |
| `--overwrite` | also re-resolve rows that already have a website |
| `--apply` | actually spend and write. Without it: nothing happens |

City and state are not optional decoration — they are what separates the Florida
clinic from the Kentucky one with the same name, and they are passed to the LLM
judge so namesakes in other cities get NONE'd. Point `--col_city`/`--col_state`
at real data.

Rejected before the LLM ever sees them, with the reason recorded: directories
and aggregators (healthcare family, and since 2026-08-10 the production family —
productionhub, mandy, staffmeup, imdb, vimeo, behance, peerspace, giggster,
clutch, sortlist…), job boards, social, site builders, `.gov`,
`careers.`/`jobs.` subdomains, and any domain already claimed by a different
company on the same sheet (the parent-chain trap). These mechanical guards are
what keep the LLM judge from repeating find_company_domains.py's measured
failure (28 HCA facilities → careers.hcahealthcare.com).

## Cost and credits

~**$0.007 per company** (Exa's measured `costDollars.total`), so ~$7 per 1,000.
The dry run prints the estimate before anything is spent. Page text comes back
in the *same* search call — asking `/contents` separately would double the bill
for no extra information.

- **402 = out of credits.** The script stops immediately rather than burning the
  remainder of the run. Top up at dashboard.exa.ai.
- **401 = bad key.** Set `EXA_API_KEY` in `.claude/.env`.

## Reading the output

The summary prints a status histogram and lists every `ok_verify` by name. Those
are the rows worth a human glance — they are where a wrong domain would have
come from. `error:*` counts matter too: a blank caused by an HTTP failure is a
retry, a blank caused by `no_match` is a company with no findable site.

## Where this skill ends

At column L. It does not touch DM discovery, emails or campaigns — feed the
resolved sheet to `apollo-dm-waterfall` afterwards. Keeping it separate is the
point: domain resolution is where the wrong-company errors originate, so it gets
its own gate and its own audit rather than being buried inside an enrichment run.
