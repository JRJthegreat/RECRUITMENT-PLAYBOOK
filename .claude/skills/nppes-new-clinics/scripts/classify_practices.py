"""
Phase 2 — false-positive classification + scoring.

A new Type-2 NPI does not reliably mean "a new practice is opening" —
subparts, billing reorganizations, ownership changes, and solo-clinician
PLLCs all look identical in the raw feed. This phase tags every pulled row:

  NEW_INDEPENDENT  no parent/subpart, novel address, no name match  <- the target
  NEW_LOCATION     existing brand (fuzzy name match) at a new address
  LIKELY_ADMIN     subpart, parent org present, or address already active
  UNCERTAIN        baseline missing -> manual review

plus a deterministic solo-PLLC flag (authorized official's surname inside the
org name, or a clinical credential inside a short org name), then scores each
row with the weights in config/settings.json.

Requires the baseline (build_baseline.py) for address novelty / name match —
without it everything classifies UNCERTAIN. Warns when the baseline is stale.

Usage:
  python3 -W ignore .claude/skills/nppes-new-clinics/scripts/classify_practices.py
  ... [--limit N] [--dry_run] [--reclassify]

--dry_run prints what would be tagged, writes nothing.
--reclassify re-runs rows that already have a classification.
"""
import argparse
import os
import sys
from datetime import date, datetime
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nppes_common import (get_db, get_meta, load_allowlist, load_settings,
                          log_run, detect_solo, normalize_org_name,
                          pick_anchor_token)


def check_baseline(conn):
    """Returns the set of states the baseline covers (empty = no usable
    baseline -> everything tags UNCERTAIN)."""
    built = get_meta(conn, "baseline_built_at")
    populated = conn.execute(
        "SELECT 1 FROM baseline_addresses LIMIT 1").fetchone()
    if not built or not populated:
        print("[classify] WARN no baseline — everything will tag UNCERTAIN. "
              "Run build_baseline.py first.")
        return set()
    age = (date.today() - date.fromisoformat(built)).days
    if age > 45:
        print(f"[classify] WARN baseline is {age} days old — rebuild after the "
              "next monthly release for accurate novelty checks")
    return {s for s in (get_meta(conn, "baseline_states") or "").split(",") if s}


def address_novel(conn, addr_key, own_npi):
    """1 = novel; 0 = an ORG already operates here (reorg signal);
    2 = only individual providers found here — often the founder's own Type-1
    NPI pre-registered at the new clinic, so it must NOT demote to
    LIKELY_ADMIN. Treated as novel downstream, kept distinct for review."""
    rows = conn.execute(
        "SELECT DISTINCT entity FROM baseline_addresses "
        "WHERE addr_key = ? AND npi != ? LIMIT 5",
        (addr_key, own_npi)).fetchall()
    ents = {r["entity"] for r in rows}
    if "2" in ents:
        return 0
    return 2 if ents else 1


def best_name_match(conn, name_norms, state, own_npi, threshold):
    """Match legal name AND DBA against baseline org/DBA names.

    Two passes per name: fuzzy within the same state (anchor-token prefilter),
    plus exact normalized-name equality NATIONALLY — a multi-state chain's
    first clinic in a new state has zero in-state rows but an exact name hit
    elsewhere. Returns (similarity, npi, name)."""
    best, best_npi, best_name = 0.0, "", ""
    for name_norm in name_norms:
        if not name_norm:
            continue
        hit = conn.execute(
            "SELECT npi, name_norm FROM baseline_orgs "
            "WHERE name_norm = ? AND npi != ? LIMIT 1",
            (name_norm, own_npi)).fetchone()
        if hit:
            return 1.0, hit["npi"], hit["name_norm"]
        anchor = pick_anchor_token(name_norm)
        if not anchor:
            continue
        rows = conn.execute(
            "SELECT npi, name_norm FROM baseline_orgs "
            "WHERE state = ? AND npi != ? AND name_norm LIKE ? LIMIT 4000",
            (state, own_npi, f"%{anchor}%")).fetchall()
        for r in rows:
            sm = SequenceMatcher(None, name_norm, r["name_norm"])
            if sm.quick_ratio() < max(best, threshold - 0.1):
                continue
            ratio = sm.ratio()
            if ratio > best:
                best, best_npi, best_name = ratio, r["npi"], r["name_norm"]
    return best, best_npi, best_name


def classify_row(p, conn, have_baseline, threshold):
    subpart = (p["is_subpart"] or "").upper() == "Y"
    parent = bool((p["parent_lbn"] or "").strip() or (p["parent_tin"] or "").strip())

    addr_nov, sim, m_npi, m_name = None, 0.0, "", ""
    if have_baseline:
        addr_nov = address_novel(conn, p["addr_key"], p["npi"])
        sim, m_npi, m_name = best_name_match(
            conn,
            [normalize_org_name(p["org_name"]), normalize_org_name(p["dba_name"])],
            p["state"], p["npi"], threshold)

    if addr_nov is None:
        cls = "UNCERTAIN"
    elif subpart or parent:
        # A system subpart at a NOVEL address is a new site opening — high
        # value (budget, repeat buyer, one relationship spans future sites).
        # At an address the parent already operates, it is a billing reorg.
        cls = "HEALTH_SYSTEM_EXPANSION" if addr_nov else "LIKELY_ADMIN"
    elif addr_nov == 0:              # an org already operates at this address
        cls = "LIKELY_ADMIN"
    elif sim >= threshold:           # addr_nov 1 or 2 = novel enough
        cls = "NEW_LOCATION"
    else:
        cls = "NEW_INDEPENDENT"

    solo, solo_reason = detect_solo(p["org_name"], p["ao_last"])
    return cls, addr_nov, sim, m_npi, m_name, solo, solo_reason


def score_row(p, cls, addr_nov, solo, settings, allowlist):
    w = settings["scoring"]
    score = w["classification"].get(cls, 0)
    days = (date.today() - date.fromisoformat(p["enumeration_date"])).days
    for cutoff, pts in w["enum_age_buckets"]:
        if days <= cutoff:
            score += pts
            break
    cat = allowlist.get(p["taxonomy_category"], {})
    score += w["shortage"].get(cat.get("shortage", "low"), 0)
    title = ((p["ao_title"] or "") + " " + (p["ao_credential"] or "")).upper()
    if any(k in title for k in settings["owner_title_keywords"]) or \
       any(c in title.split() for c in ("MD", "DO")):
        score += w["ao_title_owner"]
    if (p["is_subpart"] or "").upper() != "Y":
        score += w["not_subpart"]
    if addr_nov in (1, 2):
        score += w["novel_address"]
    if p["email"]:
        score += w["email_enriched"]
    if solo:
        score += w["solo_name_match"]
    # multi-site owner = group opening locations = hot demand, but only in the
    # growth band; above the ceiling it is an enterprise system with an MSP
    n_sites = p["owner_site_count"] or 0
    if w["multi_site_min"] <= n_sites <= w["multi_site_max"]:
        score += w["multi_site_owner"]
    return score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--reclassify", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    allowlist = load_allowlist()
    threshold = settings["name_similarity_threshold"]
    conn = get_db()
    covered_states = check_baseline(conn)

    where = ("WHERE classification IS NOT NULL AND classification != 'OFF_TAXONOMY'"
             if args.reclassify else "WHERE classification IS NULL")
    rows = conn.execute(f"SELECT * FROM practices {where} ORDER BY enumeration_date").fetchall()
    if args.limit:
        rows = rows[:args.limit]
    uncovered = sorted({p["state"] for p in rows} - covered_states)
    if covered_states and uncovered:
        print(f"[classify] WARN baseline does not cover {uncovered} — those "
              "rows tag UNCERTAIN until build_baseline.py reruns with them")
    print(f"[classify] {len(rows)} rows to classify"
          f"{' | DRY RUN' if args.dry_run else ''}")

    tallies, pending = {}, 0
    for p in rows:
        cls, addr_nov, sim, m_npi, m_name, solo, solo_reason = classify_row(
            p, conn, p["state"] in covered_states, threshold)
        score = score_row(p, cls, addr_nov, solo, settings, allowlist)
        tallies[cls] = tallies.get(cls, 0) + 1

        if args.dry_run:
            print(f"  {p['npi']} {cls:16s} score={score:+3d} solo={solo} "
                  f"sim={sim:.2f} {p['org_name'][:48]} | {p['city']}, {p['state']}")
            continue
        conn.execute(
            """UPDATE practices SET classification=?, score=?, addr_is_novel=?,
               name_similarity=?, name_match_npi=?, name_match_name=?,
               solo_flag=?, solo_reason=?, classified_at=? WHERE npi=?""",
            (cls, score, addr_nov, round(sim, 3), m_npi, m_name,
             solo, solo_reason, datetime.now().isoformat(timespec="seconds"),
             p["npi"]))
        pending += 1
        if pending >= 10:          # batch-of-10 commit
            conn.commit()
            pending = 0
    conn.commit()

    solo_n = conn.execute(
        "SELECT COUNT(*) c FROM practices WHERE solo_flag = 1").fetchone()["c"]
    summary = (f"{len(rows)} classified: " +
               ", ".join(f"{k} {v}" for k, v in sorted(tallies.items())) +
               f" | solo-flagged total in store: {solo_n}")
    print(f"[classify] {summary}")
    if not args.dry_run:
        log_run(conn, "classify_practices", summary)
    conn.close()


if __name__ == "__main__":
    main()
