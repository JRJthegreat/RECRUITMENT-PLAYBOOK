"""
Phase 2 — classify stored companies: commercial production house or not.

GPT-4.1 (Azure fast deployment) over Google Maps metadata only — name,
category labels, domain, review count. No website scrape at this stage;
that spend is reserved for rows that survive classification.

Classes:
  PRODUCTION_HOUSE  commercial/corporate/brand video production company — the ICP
  POST_ONLY         post-production only (edit/color/VFX/sound/dubbing)
  FREELANCER        one-person videographer / personal-name operation
  WEDDING_EVENT     wedding + event videography
  PHOTO_ONLY        photography studio
  EQUIPMENT_STUDIO  gear rental / studio-space rental
  AGENCY            ad / marketing / creative agency (video is a service line)
  MEDIA_OTHER       TV, radio, news, church media, schools, record labels
  UNCERTAIN         cannot tell from metadata — kept in store, not exported,
                    candidate for a website-scrape refinement pass later

The classifier leans UNCERTAIN rather than guessing: "X Films" is a wedding
videographer as often as a commercial studio. Only PRODUCTION_HOUSE rows
ever reach a campaign sheet, so a false UNCERTAIN costs a later refinement
call while a false PRODUCTION_HOUSE costs DM + email credits.

Nothing is deleted — wrong-class rows just never export.

Resume-safe: only classifies rows WHERE classification IS NULL. Commits per
batch of 20.

Usage:
  python3 -W ignore .claude/skills/production-house-leads/scripts/classify_studios.py [--limit N] [--dry_run]
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from production_common import get_db, log_run

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
AZ_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZ_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZ_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZ_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST")

BATCH = 20
CLASSES = {"PRODUCTION_HOUSE", "POST_ONLY", "FREELANCER", "WEDDING_EVENT",
           "PHOTO_ONLY", "EQUIPMENT_STUDIO", "AGENCY", "MEDIA_OTHER", "UNCERTAIN"}

SYSTEM = """You classify companies scraped from Google Maps for a B2B campaign whose ICP is \
commercial video production houses: companies that produce video content for brands, \
businesses and agencies (commercials, brand films, corporate video, branded content).

For each company you get: name, Google Maps category labels, website domain, review count, city.

Classes:
- PRODUCTION_HOUSE: a commercial/corporate/brand video production company. This includes \
full-service studios and production companies serving business clients.
- POST_ONLY: only post-production (editing, color, VFX, sound, dubbing, subtitling).
- FREELANCER: clearly a one-person operation — personal name as company name \
("John Smith Video"), "videographer" category with a personal brand.
- WEDDING_EVENT: wedding films, event videography, quinceañeras, memorial videos.
- PHOTO_ONLY: photography studio (portraits, real estate photos, headshots) without video production focus.
- EQUIPMENT_STUDIO: camera/grip rental, studio space / soundstage rental.
- AGENCY: advertising, marketing or creative agency where video is one service among many \
(SEO, branding, web design).
- MEDIA_OTHER: TV/radio stations, news outlets, church media ministries, film schools, \
record labels, cinemas, talent agencies.
- UNCERTAIN: genuinely cannot tell from the metadata.

Rules:
- "X Films" / "X Media" names alone prove nothing — use the category labels and domain.
- Wedding words anywhere (name, categories, domain) -> WEDDING_EVENT.
- A "Video production service" category with a business-looking name and no wedding/photo \
signals -> PRODUCTION_HOUSE.
- When torn between PRODUCTION_HOUSE and anything else, choose UNCERTAIN. A wrong \
PRODUCTION_HOUSE wastes enrichment credits; UNCERTAIN just waits.

Return JSON: {"results": [{"i": <index>, "class": "<CLASS>", "reason": "<max 12 words>"}, ...]} \
with exactly one entry per input index."""


def classify_batch(rows):
    lines = []
    for i, r in enumerate(rows):
        lines.append(f'{i}. name="{r["name"]}" | categories="{r["categories"] or r["category"] or ""}" '
                     f'| domain={r["domain"] or "none"} | reviews={r["reviews"] or 0} | city={r["city"] or ""}')
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
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    q = ("SELECT place_id, name, category, categories, domain, reviews, city "
         "FROM companies WHERE classification IS NULL ORDER BY metro, name")
    rows = [dict(zip(("place_id", "name", "category", "categories", "domain", "reviews", "city"), r))
            for r in conn.execute(q).fetchall()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} unclassified rows")
    if args.dry_run:
        for r in rows[:20]:
            print(f'  {r["name"]} | {r["categories"] or r["category"]} | {r["domain"]}')
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
                         "classified_at=datetime('now') WHERE place_id=?",
                         (cls, reason, r["place_id"]))
            counts[cls] = counts.get(cls, 0) + 1
            done += 1
        conn.commit()
        print(f"  {done}/{len(rows)} classified")

    print("\nBreakdown:")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:16s} {n}")
    log_run(conn, "classify_studios", f"classified {done}: {counts}")


if __name__ == "__main__":
    main()
