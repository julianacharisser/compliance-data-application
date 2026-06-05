"""
dedup/merger.py
───────────────
Merges AUTO_MERGE pairs from dedup_results.json into canonical Person entities.

MERGE STRATEGY:
    - CIA id kept as canonical id
    - sources: union of both ["cia_world_leaders", "ofac_sdn"]
    - topics: union of both ["role.pep", "sanction"]
    - name: CIA wins — verified government source
    - firstName, lastName, nameSuffix: CIA wins
    - position: union of both — preserves all known roles
    - alias: union of both
    - birthDate: OFAC wins — CIA has none
    - nationality, citizenship: OFAC wins — CIA has none
    - passportNumber, idNumber: OFAC wins — CIA has none
    - address: OFAC wins — CIA has none
    - notes: union of both
    - sourceUrl: union of both
    - retrievedAt: CIA wins (most recent scrape)
    - merged_from: [cia_id, ofac_id] — preserved for traceability

OUTPUT FILES:
    dedup/merged.json       — canonical merged Person entities
    dedup/unmerged_cia.json — CIA persons with no OFAC match
    dedup/unmerged_ofac.json— OFAC persons with no CIA match
"""

import json
import time
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

DEDUP_RESULTS_PATH   = "dedup/dedup_results.json"
CIA_NORMALIZED_PATH  = "transform/cia_normalized.json"
OFAC_NORMALIZED_PATH = "transform/ofac_normalized.json"

MERGED_OUTPUT_PATH          = "dedup/merged.json"
UNMERGED_CIA_OUTPUT_PATH    = "dedup/unmerged_cia.json"
UNMERGED_OFAC_OUTPUT_PATH   = "dedup/unmerged_ofac.json"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_persons_by_id(path: str) -> dict[str, dict]:
    """
    Loads a normalized JSON file and returns a dict keyed by person id.
    Handles both flat list format and {"persons": [...]} format.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    persons = data["persons"] if isinstance(data, dict) else data
    return {p["id"]: p for p in persons}


def get_prop(person: dict, field: str) -> list:
    """
    Safely gets a property list from a person entity.
    Returns empty list if field is missing or None.
    """
    return person.get("properties", {}).get(field) or []


def union_prop(*lists) -> list:
    """
    Merges multiple property lists into one deduplicated list.
    Preserves order — first occurrence wins for duplicates.

    Example:
        union_prop(["role.pep"], ["sanction"]) → ["role.pep", "sanction"]
        union_prop(["Ali"], ["Ali", "AKA2"])   → ["Ali", "AKA2"]
    """
    seen = set()
    result = []
    for lst in lists:
        for item in (lst or []):
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


def merge_pair(cia: dict, ofac: dict) -> dict:
    """
    Merges one CIA person and one OFAC person into a canonical entity.

    Merge rules by field:
        CIA wins:   name, firstName, lastName, nameSuffix, retrievedAt
        OFAC wins:  birthDate, nationality, citizenship, address,
                    passportNumber, idNumber
        Union:      position, alias, topics, notes, sourceUrl, sources
        Canonical:  id = CIA id, schema = "Person"
        Preserved:  merged_from = [cia_id, ofac_id]

    Args:
        cia:  CIA normalized Person dict
        ofac: OFAC normalized Person dict

    Returns:
        Merged canonical Person dict
    """
    return {
        "schema": "Person",

        # CIA id is canonical — stable, government-verified source
        "id": cia["id"],

        "properties": {
            # ── Name — CIA wins ───────────────────────────────────────────────
            # CIA uses verified official name from government records
            "name":       get_prop(cia, "name") or get_prop(ofac, "name"),
            "firstName":  get_prop(cia, "firstName") or get_prop(ofac, "firstName"),
            "lastName":   get_prop(cia, "lastName") or get_prop(ofac, "lastName"),
            "nameSuffix": get_prop(cia, "nameSuffix") or get_prop(ofac, "nameSuffix"),

            # ── Aliases — union ───────────────────────────────────────────────
            # OFAC has many aliases, CIA rarely has any — combine both
            "alias": union_prop(get_prop(cia, "alias"), get_prop(ofac, "alias")),

            # ── Position — union ──────────────────────────────────────────────
            # CIA has current government role, OFAC may have historical roles
            "position": union_prop(get_prop(cia, "position"), get_prop(ofac, "position")),

            # ── Title/honorific — union ───────────────────────────────────────
            "title": union_prop(get_prop(cia, "title"), get_prop(ofac, "title")),

            # ── Topics — union ────────────────────────────────────────────────
            # Merged entity is both a PEP and sanctioned
            # ["role.pep"] + ["sanction"] → ["role.pep", "sanction"]
            "topics": union_prop(get_prop(cia, "topics"), get_prop(ofac, "topics")),

            # ── Dates — OFAC wins ─────────────────────────────────────────────
            # CIA has no birth dates — OFAC is the only source
            "birthDate": get_prop(ofac, "birthDate"),

            # ── Country — OFAC wins ───────────────────────────────────────────
            # CIA does not store nationality on persons (uses country metadata)
            # OFAC has structured nationality and citizenship fields
            "nationality": get_prop(ofac, "nationality"),
            "citizenship": get_prop(ofac, "citizenship"),

            # ── Identity documents — OFAC wins ────────────────────────────────
            # CIA has no passport/ID data
            "passportNumber": get_prop(ofac, "passportNumber"),
            "idNumber":       get_prop(ofac, "idNumber"),

            # ── Address — OFAC wins ───────────────────────────────────────────
            # CIA has no address data
            "address": get_prop(ofac, "address"),

            # ── Notes — union ─────────────────────────────────────────────────
            # CIA notes may have context, OFAC notes have programs + remarks
            "notes": union_prop(get_prop(cia, "notes"), get_prop(ofac, "notes")),

            # ── Source metadata ───────────────────────────────────────────────
            # Union of both source URLs
            "sourceUrl":   union_prop(get_prop(cia, "sourceUrl"), get_prop(ofac, "sourceUrl")),

            # CIA retrievedAt — most recent government scrape
            "retrievedAt": get_prop(cia, "retrievedAt") or get_prop(ofac, "retrievedAt"),
        },

        # ── Top-level metadata ────────────────────────────────────────────────
        "sources": ["cia_world_leaders", "ofac_sdn"],

        # Preserve CIA country metadata for API filtering
        "country":      cia.get("country", ""),
        "country_code": cia.get("country_code", ""),
        "date_updated": cia.get("date_updated", ""),

        # Preserve OFAC uid for cross-referencing
        "ofac_uid": ofac.get("ofac_uid", ""),

        # Traceability — which original records were merged
        "merged_from": [cia["id"], ofac["id"]],
    }


# ─── Main Run Function ────────────────────────────────────────────────────────

def run():
    """
    Loads dedup results and normalized persons, merges AUTO_MERGE pairs,
    and writes three output files:
        merged.json        — canonical merged entities
        unmerged_cia.json  — CIA persons with no OFAC match
        unmerged_ofac.json — OFAC persons with no CIA match

    Returns dict with all three lists for FastAPI pipeline use.
    """
    start = time.time()

    print("Loading data...")
    with open(DEDUP_RESULTS_PATH, encoding="utf-8") as f:
        dedup_results = json.load(f)

    cia_by_id  = load_persons_by_id(CIA_NORMALIZED_PATH)
    ofac_by_id = load_persons_by_id(OFAC_NORMALIZED_PATH)

    print(f"  CIA persons:    {len(cia_by_id)}")
    print(f"  OFAC persons:   {len(ofac_by_id)}")
    print(f"  Dedup results:  {len(dedup_results)}")

    # ── Collect AUTO_MERGE pairs ──────────────────────────────────────────────
    auto_merge_pairs = [
        r for r in dedup_results
        if r["recommended_action"] == "AUTO_MERGE"
    ]
    print(f"  AUTO_MERGE pairs: {len(auto_merge_pairs)}\n")

    # ── Merge ─────────────────────────────────────────────────────────────────
    print("Merging pairs...")
    merged = []
    merged_cia_ids  = set()
    merged_ofac_ids = set()
    failed = 0

    for pair in auto_merge_pairs:
        cia_id  = pair["cia_id"]
        ofac_id = pair["ofac_id"]

        cia_person  = cia_by_id.get(cia_id)
        ofac_person = ofac_by_id.get(ofac_id)

        if not cia_person:
            print(f"  [WARN] CIA id not found: {cia_id}")
            failed += 1
            continue
        if not ofac_person:
            print(f"  [WARN] OFAC id not found: {ofac_id}")
            failed += 1
            continue

        # One CIA person can match multiple OFAC records — skip if already merged
        # Keep only the highest-scoring match (dedup_results is sorted by score)
        if cia_id in merged_cia_ids:
            continue

        merged_entity = merge_pair(cia_person, ofac_person)
        merged.append(merged_entity)

        merged_cia_ids.add(cia_id)
        merged_ofac_ids.add(ofac_id)

    print(f"  Merged:  {len(merged)}")
    if failed:
        print(f"  Failed:  {failed}")

    # ── Unmerged ──────────────────────────────────────────────────────────────
    # CIA persons with no OFAC match — current leaders not on sanctions list
    unmerged_cia = [
        p for pid, p in cia_by_id.items()
        if pid not in merged_cia_ids
    ]

    # OFAC persons with no CIA match — sanctioned individuals not in government
    unmerged_ofac = [
        p for pid, p in ofac_by_id.items()
        if pid not in merged_ofac_ids
    ]

    print(f"  Unmerged CIA:  {len(unmerged_cia)}")
    print(f"  Unmerged OFAC: {len(unmerged_ofac)}")

    # ── Save outputs ──────────────────────────────────────────────────────────
    Path("dedup").mkdir(exist_ok=True)

    with open(MERGED_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    with open(UNMERGED_CIA_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(unmerged_cia, f, ensure_ascii=False, indent=2)

    with open(UNMERGED_OFAC_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(unmerged_ofac, f, ensure_ascii=False, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    total = len(merged) + len(unmerged_cia) + len(unmerged_ofac)

    print(f"\n{'═' * 50}")
    print(f"  MERGER COMPLETE")
    print(f"{'═' * 50}")
    print(f"  Merged entities:    {len(merged)}")
    print(f"  Unmerged CIA:       {len(unmerged_cia)}")
    print(f"  Unmerged OFAC:      {len(unmerged_ofac)}")
    print(f"  Total entities:     {total}")
    print(f"  Completed in:       {int(elapsed // 60)}m {int(elapsed % 60)}s")
    print(f"{'═' * 50}")
    print(f"\n  Output files:")
    print(f"    {MERGED_OUTPUT_PATH}")
    print(f"    {UNMERGED_CIA_OUTPUT_PATH}")
    print(f"    {UNMERGED_OFAC_OUTPUT_PATH}")

    return {
        "merged":         merged,
        "unmerged_cia":   unmerged_cia,
        "unmerged_ofac":  unmerged_ofac,
    }


# ─── Entry Point ──────────────────────────────────────────────────────────────
# Only runs when executed directly: python dedup/merger.py
# Does NOT run when imported by db/loader.py or app.py

if __name__ == "__main__":
    run()