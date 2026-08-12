"""
Phase 2 (Claude-judge variant) — export enriched-but-still-UNCERTAIN rows for
Claude to classify in-session, instead of the GPT-4.1 call in
classify_directory.py.

Jude's call (2026-08-11): no LLM API for this decision either, same as the
website-resolution rework — Claude reads the candidates and judges directly.

Only rows with enriched_at IS NOT NULL (a full profile-page description, not
just the listing snippet) are eligible — those are the ones worth a second
look; still-UNCERTAIN rows with only the thin snippet need enrich_profiles.py
first, not a re-judge on the same thin evidence.

Same 9-class vocabulary and judging rules as classify_directory.py's SYSTEM
prompt (see that file's docstring for the full class definitions) — dumped
into this script's output so a judging agent has everything it needs without
reading classify_directory.py.

Usage:
  python3 -W ignore collect_uncertain_for_claude.py [--limit N] \
      [--out uncertain_candidates.json]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from directory_common import get_db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="uncertain_candidates.json")
    args = ap.parse_args()

    conn = get_db()
    q = ("SELECT profile_id, name, categories, city, member_since, description "
         "FROM companies WHERE classification='UNCERTAIN' AND enriched_at IS NOT NULL "
         "ORDER BY metro, name")
    rows = conn.execute(q).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    out = [{"profile_id": r[0], "name": r[1], "categories": r[2],
           "city": r[3], "member_since": r[4], "description": r[5]}
          for r in rows]

    print(f"{len(out)} enriched UNCERTAIN rows -> {args.out}")
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print("Next: have Claude judge these rows (see classify_directory.py's SYSTEM "
         "prompt for the class vocabulary and rules), write verdicts JSON "
         '([{"profile_id": "...", "class": "PRODUCTION_HOUSE", "reason": "..."}]), '
         "then apply with apply_claude_classifications.py")


if __name__ == "__main__":
    main()
