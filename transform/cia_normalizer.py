"""
transform/normalize_cia.py

Normalizes raw CIA World Leaders data into FtM-compatible Person and Occupancy entities.

INPUT:  ingest/cia_raw.json
OUTPUT: transform/cia_normalized.json

WHAT CIA DATA GIVES US:
─────────────────────────────────────────────────────
Country level (metadata — not person properties):
    country           → Occupancy.organization + to_iso_country() → Person.nationality
    country_code      → backup reference, not used directly
    date_updated      → metadata only, stored at country level
    time_updated      → metadata only, stored at country level
    caveat            → country-level note, not a person property
    diplomatic_exchange → country-level note, not a person property

Per leader:
    name              → split_cia_name() → Person.firstName + Person.lastName + Person.name
    title             → expand_abbreviations() → Person.position + Occupancy.role
    honorific         → Person.title (e.g. "Dr.", "Gen.", "Lt. Gen.", "Sir")

WHAT WE CANNOT GET FROM CIA:
    birthDate         → not available
    alias             → not available
    passportNumber    → not available
    idNumber          → not available
    topics            → we tag all CIA persons as "role.pep" (Politically Exposed Person)
                        because by definition everyone on this list holds public office

FtM ENTITY STRUCTURE:
─────────────────────────────────────────────────────
One CIA country record produces:
    - One Person entity per unique leader name
      (same person holding multiple positions = one Person, multiple Occupancies)
    - One Occupancy entity per position held

CUSTOM METADATA (outside FtM properties):
    sources           → ["cia_world_leaders"] — used in dedup step to track origin
    country           → country name string — used in dedup step for matching
    country_code      → ISO alpha-2 — used in dedup step for matching
    date_updated      → when CIA last updated this country page

SKIPPED RECORDS:
    - VACANT positions (31 found in profiler) — no person to record
    - Empty names (30 found in profiler) — data quality issue
"""

import json
import time
from helpers import (
    make_id,
    to_iso_country,
    expand_abbreviations,
    split_cia_name,
)

# ─── Constants ────────────────────────────────────────────────────────────────

CIA_INPUT_PATH  = "ingest/cia_raw.json"
CIA_OUTPUT_PATH = "transform/cia_normalized.json"
CIA_BASE_URL    = "https://www.cia.gov/resources/world-leaders/foreign-governments"


# ─── Entity Builders ──────────────────────────────────────────────────────────

def build_person(leader, country, country_code, slug):
    """
    Builds a FtM Person entity from one CIA leader record.

    FtM properties used:
        name        (inherited from Thing)   → full display name
        firstName   (Person)                 → from split_cia_name()
        lastName    (Person)                 → from split_cia_name()
        title       (Person)                 → honorific e.g. "Dr.", "Gen."
        position    (Person)                 → expanded job title
        nationality (Person)                 → ISO alpha-2 from country name
        topics      (Person)                 → "role.pep" — politically exposed person
        sourceUrl   (inherited from Entity)  → CIA country page URL

    Custom metadata (outside FtM properties):
        sources      → ["cia_world_leaders"]
        country      → raw country name string
        country_code → ISO alpha-2 code

    Args:
        leader:       dict with name, title, honorific from CIA raw data
        country:      country name string e.g. "Philippines"
        country_code: ISO alpha-2 e.g. "ph"
        slug:         URL slug e.g. "philippines"

    Returns:
        tuple (person_id, person_dict) or (None, None) if record should be skipped
    """
    raw_name  = leader.get("name", "").strip()
    raw_title = leader.get("title", "").strip()
    honorific = leader.get("honorific", "").strip()

    # Skip VACANT and empty names — 31 and 30 found in profiler respectively
    if not raw_name or "VACANT" in raw_name.upper():
        print(f"  [SKIP] VACANT or empty name — title: '{raw_title}' in {country}")
        return None, None

    first, last = split_cia_name(raw_name)

    # Build display name from split components
    if first and last:
        full_name = f"{first} {last}"
    elif last:
        full_name = last
    else:
        full_name = raw_name  # fallback — use raw if split failed

    expanded_title = expand_abbreviations(raw_title)
    iso_country    = to_iso_country(country)

    # Deterministic ID — based on source + country + raw name
    # Using raw_name (not cleaned) ensures the ID is stable across runs
    person_id = make_id("cia", country, raw_name)

    person = {
        # ── FtM properties ────────────────────────────────────────────────────
        "schema": "Person",
        "id": person_id,
        "properties": {
            # Thing (inherited)
            "name":      [full_name],
            "notes":     [],          # nothing to add for CIA persons

            # Entity (inherited)
            "sourceUrl": [f"{CIA_BASE_URL}/{slug}/"],

            # Person
            "firstName":   [first]          if first     else [],
            "lastName":    [last]           if last      else [],
            "title":       [honorific]      if honorific else [],  # e.g. "Dr.", "Gen."
            "position":    [expanded_title] if expanded_title else [],
            "nationality": [iso_country]    if iso_country else [],
            "topics":      ["role.pep"],    # everyone on CIA list is a PEP by definition
        },

        # ── Custom metadata (outside FtM) ─────────────────────────────────────
        # Used in dedup step to match persons across sources
        "sources":      ["cia_world_leaders"],
        "country":      country,
        "country_code": iso_country,
    }

    return person_id, person


def build_occupancy(leader, person_id, country):
    """
    Builds a FtM Occupancy entity linking a Person to their role.

    One Occupancy per position held. Same person holding two positions
    gets two Occupancy entities both pointing to the same Person ID.

    FtM properties used:
        holder       (Occupancy) → Person entity ID
        role         (Occupancy) → expanded job title
        organization (Occupancy) → country name

    Not available from CIA data (left empty):
        startDate    (Occupancy) → not in CIA data
        endDate      (Occupancy) → not in CIA data
        status       (Occupancy) → not in CIA data

    Custom metadata (outside FtM):
        sources → ["cia_world_leaders"]

    Args:
        leader:    dict with title from CIA raw data
        person_id: ID of the Person entity this Occupancy belongs to
        country:   country name string

    Returns:
        occupancy dict
    """
    raw_title      = leader.get("title", "").strip()
    expanded_title = expand_abbreviations(raw_title)

    # Occupancy ID includes title so same person with two roles gets two IDs
    occ_id = make_id("cia", country, leader.get("name", ""), raw_title)

    return {
        # ── FtM properties ────────────────────────────────────────────────────
        "schema": "Occupancy",
        "id": occ_id,
        "properties": {
            # Thing (inherited)
            "name":  [],   # not needed — role + organization already describe it
            "notes": [],

            # Occupancy
            "holder":       [person_id],
            "role":         [expanded_title] if expanded_title else [],
            "organization": [country],
            "startDate":    [],   # not available in CIA data
            "endDate":      [],   # not available in CIA data
            "status":       [],   # not available in CIA data
        },

        # ── Custom metadata (outside FtM) ─────────────────────────────────────
        "sources": ["cia_world_leaders"],
    }


# ─── Main Normalizer ──────────────────────────────────────────────────────────

def normalize_country(country_data):
    """
    Normalizes one CIA country record into Person and Occupancy entities.

    Handles the 476 duplicate names found in profiler:
    Same person holding multiple positions → one Person, multiple Occupancies.
    We track seen person IDs per country to avoid duplicate Person entities.

    Args:
        country_data: one country dict from cia_raw.json

    Returns:
        tuple (persons list, occupancies list)
    """
    country      = country_data["country"]
    country_code = country_data.get("country_code", "").lower()
    leaders      = country_data.get("leaders", [])

    # Derive slug from country_code for sourceUrl
    # e.g. country_code "RP" → we use the country name lowercased as slug
    slug = country.lower().replace(" ", "-").replace(",", "").replace(".", "")

    persons      = {}   # person_id → person dict (deduplicated by ID)
    occupancies  = []

    for leader in leaders:
        person_id, person = build_person(leader, country, country_code, slug)

        if person_id is None:
            continue  # VACANT or empty name — already logged in build_person

        # Only add Person if not seen yet for this country
        # This handles the 476 duplicate names from profiler
        if person_id not in persons:
            persons[person_id] = person
        
        # Always add Occupancy — same person can hold multiple roles
        occ = build_occupancy(leader, person_id, country)
        occupancies.append(occ)

    return list(persons.values()), occupancies


# ─── Run Function ─────────────────────────────────────────────────────────────

def run():
    """
    Loads cia_raw.json, normalizes all records, saves to cia_normalized.json.

    Output structure:
    {
        "meta": {
            "total_countries": 199,
            "total_persons": ...,
            "total_occupancies": ...,
            "skipped": ...,
        },
        "persons": [...],
        "occupancies": [...]
    }

    Returns:
        dict with persons and occupancies lists so FastAPI can use directly
        without reading from disk.
    """
    start = time.time()

    print(f"Loading {CIA_INPUT_PATH}...")
    with open(CIA_INPUT_PATH) as f:
        cia_data = json.load(f)
    print(f"  Loaded {len(cia_data)} countries\n")

    all_persons     = []
    all_occupancies = []
    skipped         = 0
    failed          = []

    for i, country_data in enumerate(cia_data, start=1):
        country = country_data.get("country", "?")
        print(f"  [{i}/{len(cia_data)}] {country}")

        try:
            persons, occupancies = normalize_country(country_data)
            all_persons.extend(persons)
            all_occupancies.extend(occupancies)

            # Count skipped = leaders - (persons + occupancies mismatch)
            total_leaders = len(country_data.get("leaders", []))
            skipped += total_leaders - len(occupancies)

            print(f"    {len(persons)} persons, {len(occupancies)} occupancies")

        except Exception as e:
            failed.append(country)
            print(f"  [FAIL] {country}: {e}")

    # ── Build output ──────────────────────────────────────────────────────────
    output = {
        "meta": {
            "total_countries":   len(cia_data),
            "total_persons":     len(all_persons),
            "total_occupancies": len(all_occupancies),
            "skipped":           skipped,
            "failed_countries":  failed,
        },
        "persons":     all_persons,
        "occupancies": all_occupancies,
    }

    with open(CIA_OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    elapsed = time.time() - start
    print(f"\n{'─' * 55}")
    print(f"  Countries processed : {len(cia_data)}")
    print(f"  Persons created     : {len(all_persons)}")
    print(f"  Occupancies created : {len(all_occupancies)}")
    print(f"  Records skipped     : {skipped}")
    if failed:
        print(f"  Countries failed    : {failed}")
    print(f"  Saved to            : {CIA_OUTPUT_PATH}")
    print(f"  Completed in        : {int(elapsed//60)}m {int(elapsed%60)}s")

    return output


# ─── Entry Point ──────────────────────────────────────────────────────────────
# Only runs when executed directly: python transform/normalize_cia.py
# Does NOT run when imported by the FastAPI pipeline

if __name__ == "__main__":
    run()