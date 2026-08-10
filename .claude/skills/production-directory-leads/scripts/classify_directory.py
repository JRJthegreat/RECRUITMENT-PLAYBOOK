"""
Phase 2 — classify stored profiles: commercial production house or not.

GPT-4.1 (Azure fast deployment) over ProductionHub listing metadata — name,
directory categories, city, member-since, and the profile's SELF-WRITTEN
description snippet. That description is the big advantage over the Maps
pipeline: companies say what they are ("full-service production company for
brands and agencies" vs "I am a Director/DP", "wedding films"), so far fewer
rows should land UNCERTAIN.

Same class vocabulary as production-house-leads/classify_studios.py, so both
stores can be compared/merged downstream.

Classes:
  PRODUCTION_HOUSE  commercial/corporate/brand video production company — the ICP
  POST_ONLY         post-production only (edit/color/VFX/sound/dubbing)
  FREELANCER        one-person videographer / DP / director with a personal brand
  WEDDING_EVENT     wedding + event videography
  PHOTO_ONLY        photography studio
  EQUIPMENT_STUDIO  gear rental / studio-space / soundstage rental
  AGENCY            ad / marketing / creative agency (video is a service line)
  MEDIA_OTHER       TV, radio, news, church media, schools, record labels
  UNCERTAIN         cannot tell — kept in store, candidate for enrich_profiles.py
                    --classes UNCERTAIN (full description) then --retry_uncertain

The classifier leans UNCERTAIN rather than guessing. Only PRODUCTION_HOUSE
rows ever reach a campaign sheet; a false UNCERTAIN costs a refinement pass,
a false PRODUCTION_HOUSE costs DM + email credits.

Nothing is deleted — wrong-class rows just never export.

Resume-safe: only classifies rows WHERE classification IS NULL (or, with
--retry_uncertain, rows still UNCERTAIN that have been enriched since).
Commits per batch of 20.

Usage:
  python3 -W ignore .claude/skills/production-directory-leads/scripts/classify_directory.py \
      [--limit N] [--retry_uncertain] [--dry_run]
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from directory_common import get_db, log_run

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
AZ_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZ_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZ_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST")

BATCH = 20
CLASSES = {"PRODUCTION_HOUSE", "POST_ONLY", "FREELANCER", "WEDDING_EVENT",
           "PHOTO_ONLY", "EQUIPMENT_STUDIO", "AGENCY", "MEDIA_OTHER", "UNCERTAIN"}

SYSTEM = """You classify companies from the ProductionHub directory for a B2B campaign whose ICP is \
commercial video production houses: companies that produce video content for brands, \
businesses and agencies (commercials, brand films, corporate video, branded content).

For each company you get: name, ProductionHub category slugs it is listed under, city, \
the year it joined the directory, and its self-written profile description (may be truncated).

Classes:
- PRODUCTION_HOUSE: a commercial/corporate/brand video production company. This includes \
full-service studios and production companies serving business clients.
- POST_ONLY: only post-production (editing, color, VFX, sound, dubbing, subtitling).
- FREELANCER: a one-person operation — first-person descriptions ("I am a Director/DP…", \
"Owner-operator with…"), a personal name as the company, a solo cinematographer/DP/director.
- WEDDING_EVENT: wedding films, event videography, quinceañeras, memorial videos.
- PHOTO_ONLY: photography (portraits, real estate, headshots) without video production focus.
- EQUIPMENT_STUDIO: camera/grip rental, studio space / soundstage / location rental.
- AGENCY: advertising, marketing or creative agency where video is one service among many \
(SEO, branding, web design).
- MEDIA_OTHER: TV/radio, news, church media, film schools, record labels, talent agencies, \
music/audio recording studios.
- UNCERTAIN: genuinely cannot tell.

Rules:
- The description is self-written marketing copy — read what they DO, not how they brag.
- First-person singular voice ("I", "my portfolio", "hire me") -> FREELANCER even if they \
list company-like services.
- Directory category slugs are weak evidence (companies pick many); the description wins \
when they disagree.
- Wedding words anywhere -> WEDDING_EVENT.
- When torn between PRODUCTION_HOUSE and anything else, choose UNCERTAIN. A wrong \
PRODUCTION_HOUSE wastes enrichment credits; UNCERTAIN just waits.

Return JSON: {"results": [{"i": <index>, "class": "<CLASS>", "reason": "<max 12 words>"}, ...]} \
with exactly one entry per input index."""


def classify_batch(rows):
    lines = []
    for i, r in enumerate(rows):
        desc = (r["description"] or "").replace("\n", " ")[:400]
        lines.append(f'{i}. name="{r["name"]}" | categories={r["categories"] or ""} '
                     f'| city={r["city"] or ""} | member_since={r["member_since"] or ""} '
                     f'| description="{desc}"')
    try:
        resp = requests.post(
            f"{AZ_ENDPOINT}/openai/deployments/{AZ_MODEL}/chat/completions",
            params={"api-version": AZ_VERSION},
            headers={"api-key": AZ_KEY, "Content-Type": "application/json"},
            json={"messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": "\n".join(lines)}],
                  "max_completion_tokens": 2000,
                  "response_format": {"type": "json_object"}},
            timeout=120)
        if resp.status_code != 200:
            print(f"  [!] Azure HTTP {resp.status_code}: {resp.text[:120]}")
            return None
        out = json.loads(resp.json()["choices"][0]["message"]["content"])
        return {r["i"]: (r["class"], r.get("reason", "")) for r in out.get("results", [])
                if r.get("class") in CLASSES}
    except Exception as e:
        print(f"  [!] {type(e).__name__}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap PENDING rows processed")
    ap.add_argument("--retry_uncertain", action="store_true",
                    help="re-classify UNCERTAIN rows that have been enriched (full description)")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    if args.retry_uncertain:
        where = "classification='UNCERTAIN' AND enriched_at IS NOT NULL AND classified_at < enriched_at"
    else:
        where = "classification IS NULL"
    q = (f"SELECT profile_id, name, categories, city, member_since, description "
         f"FROM companies WHERE {where} ORDER BY metro, name")
    rows = [dict(zip(("profile_id", "name", "categories", "city", "member_since", "description"), r))
            for r in conn.execute(q).fetchall()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} rows to classify")
    if args.dry_run:
        for r in rows[:20]:
            print(f'  {r["name"]} | {r["categories"]} | {(r["description"] or "")[:60]}')
        print("Dry run — no LLM calls, no writes.")
        return

    done = 0
    counts = {}
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        result = classify_batch(batch)
        if result is None:
            print(f"  batch @{start} failed — skipping (rerun resumes here)")
            continue
        for i, r in enumerate(batch):
            cls, reason = result.get(i, ("UNCERTAIN", "no verdict returned"))
            conn.execute("UPDATE companies SET classification=?, class_reason=?, "
                         "classified_at=datetime('now') WHERE profile_id=?",
                         (cls, reason, r["profile_id"]))
            counts[cls] = counts.get(cls, 0) + 1
            done += 1
        conn.commit()
        print(f"  {done}/{len(rows)} classified")

    print("\nBreakdown:")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:16s} {n}")
    log_run(conn, "classify_directory", f"classified {done}: {counts}")


if __name__ == "__main__":
    main()
