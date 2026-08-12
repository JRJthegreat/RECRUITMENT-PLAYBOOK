"""
Apply Claude's classification verdicts (from collect_uncertain_for_claude.py)
to the SQLite store — no LLM API call in this path at all.

Verdicts file: JSON array [{"profile_id": "...", "class": "PRODUCTION_HOUSE",
"reason": "..."}, ...]. A class outside the 9-vocabulary set is refused
(same "refuse invalid verdicts" discipline as the Exa apply step).

Usage:
  python3 -W ignore apply_claude_classifications.py --verdicts verdicts.json [--dry_run]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from directory_common import get_db, log_run

CLASSES = {"PRODUCTION_HOUSE", "POST_ONLY", "FREELANCER", "WEDDING_EVENT",
           "PHOTO_ONLY", "EQUIPMENT_STUDIO", "AGENCY", "MEDIA_OTHER", "UNCERTAIN"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    with open(args.verdicts) as f:
        verdicts = json.load(f)

    conn = get_db()
    counts = {}
    applied = 0
    for v in verdicts:
        pid, cls, reason = v.get("profile_id"), v.get("class"), v.get("reason", "")
        if cls not in CLASSES:
            print(f"  [!] {pid}: class {cls!r} not in vocabulary — refused")
            continue
        row = conn.execute("SELECT name FROM companies WHERE profile_id=?", (pid,)).fetchone()
        if not row:
            print(f"  [!] {pid}: not found in store — refused")
            continue
        counts[cls] = counts.get(cls, 0) + 1
        if not args.dry_run:
            conn.execute("UPDATE companies SET classification=?, class_reason=?, "
                        "classified_at=datetime('now') WHERE profile_id=?",
                        (cls, reason, pid))
        applied += 1

    if args.dry_run:
        print(f"[DRY RUN] would apply {applied} verdicts: {counts}")
        return

    conn.commit()
    log_run(conn, "apply_claude_classifications", f"applied {applied}: {counts}")
    print(f"Applied {applied} verdicts:")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cls:16s} {n}")


if __name__ == "__main__":
    main()
