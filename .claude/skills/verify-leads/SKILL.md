# verify-leads

Verified DM & email enrichment pipeline for any Google Sheet lead list.
Finds the correct company website, LinkedIn decision maker, and personal email —
rejecting bad data at every step rather than letting wrong info propagate.

## The Problem This Solves

Naive website finders accept any Google result that contains a word from the
company name. This causes "electric.ai", "placejoys.com", and industry SaaS
blogs to be used as company domains, which then poisons every downstream step
(AMF finds DMs at the wrong companies, emails bounce or go to wrong people).

This skill enforces **brand-word-only matching**: generic industry words
(electric, roofing, trucking, pawn, etc.) are excluded from domain matching.
Only specific brand words unique to the company name are allowed to trigger
a match.

## Scripts

| Script | Purpose |
|--------|---------|
| `find_websites.py` | Find company website via Google (brand-word strict + extended skip list) |
| `find_dms.py` | Find DM name/title via Google + LinkedIn snippet parsing |
| `find_emails.py` | Find personal email via AMF (person → decision-maker, no generic fallback) |
| `verify.py` | Audit existing data and optionally clear bad rows |

## Run Order

```bash
# 1. Find websites (brand-word strict)
python3 -W ignore find_websites.py \
  --sheet_url "URL" --tab "TAB" \
  --col_name 0 --col_city 1 --col_state 2 --col_website 12

# 2. Verify what was found (dry-run)
python3 -W ignore verify.py \
  --sheet_url "URL" --tab "TAB" \
  --col_name 0 --col_website 12 --col_email 16

# 3. Clear bad rows if needed
python3 -W ignore verify.py \
  --sheet_url "URL" --tab "TAB" \
  --col_name 0 --col_website 12 --col_email 16 --clear

# 4. Find DMs
python3 -W ignore find_dms.py \
  --sheet_url "URL" --tab "TAB" \
  --col_name 0 --col_website 12 \
  --col_dm_name 13 --col_dm_title 14 --col_dm_linkedin 15

# 5. Find personal emails (no info@ fallback)
python3 -W ignore find_emails.py \
  --sheet_url "URL" --tab "TAB" \
  --col_name 0 --col_website 12 \
  --col_dm_name 13 --col_email 16 --col_first 17 --col_last 18
```

## Column Args

All column indices are 0-based (A=0, B=1, ...).
Scripts skip rows that already have data in the target column — fully resumable.

## Applied To SBA Borrowers

```bash
SHEET="https://docs.google.com/spreadsheets/d/1WgIhmQmJ1XhYHIVb6DgPuvBG1ex1_k76fPvr9BBVfR0/edit"
TAB="dataset_sba-rural-loans_2026-04-16_05-40-32-227"

python3 -W ignore find_websites.py --sheet_url "$SHEET" --tab "$TAB" --col_name 0 --col_city 1 --col_state 2 --col_website 12
python3 -W ignore verify.py --sheet_url "$SHEET" --tab "$TAB" --col_name 0 --col_website 12 --col_email 16
python3 -W ignore find_dms.py --sheet_url "$SHEET" --tab "$TAB" --col_name 0 --col_website 12 --col_dm_name 13 --col_dm_title 14 --col_dm_linkedin 15
python3 -W ignore find_emails.py --sheet_url "$SHEET" --tab "$TAB" --col_name 0 --col_website 12 --col_dm_name 13 --col_email 16 --col_first 17 --col_last 18
```
