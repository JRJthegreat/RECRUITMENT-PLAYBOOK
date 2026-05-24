# healthcare-leads-indeed

Scrapes Indeed for private practices and clinics actively hiring Family Medicine Physicians, Nurse Practitioners, and Physician Assistants in NY and MD. Introduces qualified leads to healthcare staffing agencies for a monthly placement fee.

---

## Target Market

**Demand side:** Small private practices and clinics (≤500 employees) hiring clinical staff who lack an internal TA team.

**Supply side:** Healthcare staffing agencies registered in NY and MD.

**NOT targets:** Hospital systems, large chains, FQHCs, government facilities, staffing agencies posting on behalf of clients.

---

## Keywords × City Grid

**Keywords (4):**
- `Family Medicine Physician`
- `Family Practice Physician`
- `Nurse Practitioner`
- `Physician Assistant`

**New York cities (12):**
New York City, Brooklyn, Queens, Bronx, Staten Island, Long Island, White Plains, Yonkers, Buffalo, Rochester, Albany, Syracuse

**Maryland cities (10):**
Baltimore, Rockville, Silver Spring, Bethesda, Gaithersburg, Columbia, Annapolis, Frederick, Germantown, Towson

**Total:** 4 × 22 = **88 actor runs** per full scrape

---

## Scraper Settings

- **Actor:** `valig/indeed-jobs-scraper`
- **Limit:** 1000 per combo (never 100 or 200 — always 1000)
- **Employee cap:** NONE at scrape time — let filtering steps handle it
- **Date filter:** NONE at scrape time — filter manually after
- **Country:** `us`

---

## Pipeline — Correct Execution Order

Run each step in this exact sequence:

### Step 1 — Scrape
```bash
python3 -W ignore scrape_and_pull.py --yes
```
Creates a new Google Sheet. Writes all unique job postings (no size or date filter).

### Step 2 — Sort by Date Published (oldest first)
Sort column E (Date Published) **ascending** via Sheets API or manually in the sheet. Oldest posts = highest pain signal = top of list.

### Step 3 — Delete pre-2026 rows
Remove all rows where Date Published does not start with `2026`. Keeps only current-year postings.

### Step 4 — Remove by company name keywords (fast, free)
Delete rows where company name contains:
- **Health system brands:** `hca` (⚠️ use word boundary `\bhca\b` — substring matches "healthcare"), `ascension`, `kaiser`, `commonspirit`, `tenet`, `trinity health`
- **University:** `university`
- **Obvious agencies:** `staffing`, `locum`, `recruiting`, `recruitment`, `travel nurs`

⚠️ `HCA` must be matched as a whole word only — it appears as a substring in "healthcare" and will create false positives if matched naively.

### Step 5 — Remove staffing agencies (LLM)
Run `classify_companies.py` with **only `agency` in DROP_CATEGORIES** (temporarily set `DROP_CATEGORIES = {"agency"}`):
```bash
python3 -W ignore classify_companies.py --sheet_url "URL" --apply
```
Restore `DROP_CATEGORIES` to full set after running.

### Step 6 — Remove >500 employees
Delete rows where column M (Company Size) lower bound > 500. **Keep rows with blank/missing size** — blank usually means a very small practice that didn't publish headcount on Indeed.

### Step 7 — Dedupe by company
```bash
python3 -W ignore dedupe_by_company.py --sheet_url "URL" --apply
```
Keeps one row per practice. Priority: Physician (tier 9) > NP (tier 7) > PA (tier 6). Tiebreak: oldest Date Published wins.

---

## Dedupe Logic Detail

- **Normalize company name:** strips legal suffixes (LLC, PLLC, PC, LLP), medical suffixes (Medical, Health, Clinic, Practice, Center), punctuation, and capitalization before grouping
- **Winner = highest role tier** — Physician beats NP beats PA regardless of posting age
- **Tiebreak = oldest posting** — within the same tier, longer-open role = more pain = keep it
- **Deletion is bottom-up** so row indices stay stable during batch deletes

---

## Column Schema (29 columns)

```
A: Job_Id
B: Job Title
C: Job Type
D: Occupations
E: Date Published          ← sort ascending; delete pre-2026
F: Salary Min
G: Salary Max
H: Salary Period
I: Apply URL (https://indeed.com/viewjob?jk={Job_Id})
J: Job Description
K: Company Name            ← keyword filter runs here
L: Company Website         ← filled by find_company_domains.py
M: Company Size            ← keep blanks; delete >500
N: Revenue
O: CEO Name
P: Company Description
Q: Benefits
R: City
S: State
T: DM Name                 ← filled by find_dm.py / enrich_emails.py
U: DM Title
V: LinkedIn URL
W: Email                   ← filled by enrich_emails.py
X: First Name
Y: Last Name
Z: Body                    ← Jude fills manually
AA: Added to Instantly     ← filled by push_campaign.py
```

---

## DM Routing (find_dm.py) — 3-Pass Fixed

| Pass | Tier | Target Titles |
|------|------|--------------|
| 1 | `practice_manager` | Practice Manager, Office Manager, Clinic Manager, Practice Administrator |
| 2 | `physician_owner` | Owner, Medical Director, CEO |
| 3 | `managing_partner` | Managing Partner, Partner |

Routing is **fixed regardless of company size** — unlike HR/tech pipelines which route by headcount.

---

## Email Enrichment (enrich_emails.py)

AMF category mapping:
- `practice_manager` → `hr` (primary) → `operations` (fallback) → `ceo` (final fallback)

No email generation — Jude writes copy manually into column Z.

---

## Scripts

| Script | Phase | Notes |
|--------|-------|-------|
| `scrape_and_pull.py` | 1 | Keyword × city grid, 1000/combo, no filters |
| `reingest_from_apify.py` | 1 (replay) | Re-pulls stored Apify datasets without new actor runs. Use `--run_count 88`. fetch limit must be 1000. |
| `classify_companies.py` | Agency filter | Run with `DROP_CATEGORIES = {"agency"}` only |
| `dedupe_by_company.py` | 7 | Physician > NP > PA; oldest tiebreak |
| `find_company_domains.py` | 1.95 | No changes from HR pipeline |
| `find_dm.py` | 2 | 3-pass fixed routing |
| `verify_dms.py` | 2.5 | No changes from HR pipeline |
| `enrich_emails.py` | 3 | AMF person + decision-maker fallback |
| `push_campaign.py` | 5 | No changes from HR pipeline |

Steps 2–6 (sort, delete pre-2026, keyword filter, agency LLM, size filter) are manual/inline Python — no dedicated scripts.

---

## Notes

- `ai_filter_jobs.py` exists but is **not part of the standard pipeline** — the keyword and size filters are sufficient and cheaper
- `reingest_from_apify.py` is useful when you need to replay the scrape with different filters without spending Apify credits — always set fetch limit to 1000
- The old `classify_companies.py` full-drop approach (hospitals + chains + FQHCs + agencies + uncertain) was replaced by the manual step sequence above
