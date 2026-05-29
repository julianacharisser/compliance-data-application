"""
transform/cia_normalizer.py

Normalizes raw CIA World Leaders data into FtM-compatible Person
and Occupancy entities.

INPUT:  ingest/cia_raw.json
OUTPUT: transform/cia_normalized.json

WHAT CIA DATA GIVES US:
    Country level (metadata — not person properties):
        country           → Occupancy.organization
        country_code      → used to detect naming convention in split_cia_name()
        date_updated      → stored in meta only
        time_updated      → stored in meta only
        caveat            → country-level note, not a person property
        diplomatic_exchange → country-level note, not a person property

    Per leader:
        name      → split_cia_name() → firstName + lastName + suffix + alias
        title     → expand_abbreviations() → Person.position + Occupancy.role
        honorific → Person.title (e.g. "Dr.", "Gen.", "Lt. Gen.")

WHAT WE CANNOT GET FROM CIA (left empty):
        birthDate, alias (from name field only), passportNumber, idNumber
        nationality — CIA shows where someone WORKS not where they're FROM
                      Setting nationality from country would be inaccurate

FtM TOPICS TAG:
        All CIA persons tagged "role.pep" — Politically Exposed Person.
        Everyone on the CIA world leaders list holds public office by definition.

CUSTOM METADATA (outside FtM properties):
        sources      → ["cia_world_leaders"]
        country      → country name string for dedup matching
        country_code → ISO alpha-2 for dedup matching
        date_updated → when CIA last updated this page

SKIPPED RECORDS:
        VACANT positions — no person to record (31 found in profiler)
        Empty names — data quality issue (30 found in profiler)
"""

import json
import time
from datetime import date
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

# Today's date for retrievedAt — set once per pipeline run
RETRIEVED_AT = date.today().isoformat()  # e.g. "2026-05-29"


# ─── Entity Builders ──────────────────────────────────────────────────────────

def build_person(leader, country, country_code, slug):
    """
    Builds a FtM Person entity from one CIA leader record.

    NAME SPLITTING:
        split_cia_name() now receives country_code so it can apply
        the correct naming strategy (East Asian, Romance, Arabic, etc.)
        Returns a dict with first_name, last_name, suffix, alias,
        embedded_honorific.

    HONORIFIC MERGING:
        CIA has two sources of honorifics:
        1. leader["honorific"] field e.g. "Dr.", "Gen."
        2. embedded in name string e.g. "Dr. Shaya al-Zindani"
        We merge both into Person.title so neither is lost.

    FtM PROPERTIES USED:
        name        (Thing)    → full display name
        firstName   (Person)   → given name(s) + middle
        lastName    (Person)   → family name only, no suffix
        nameSuffix  (Person)   → "Jr.", "Sr.", "III"
        title       (Person)   → honorific e.g. "Dr.", "Gen."
        position    (Person)   → expanded job title
        topics      (Person)   → ["role.pep"]
        sourceUrl   (Entity)   → CIA country page URL
        alias       (Person)   → from inline a.k.a. in name field
        retrievedAt (Thing)    → date this pipeline run fetched the data

    NOT SET (and why):
        nationality → we know where they work, not where they're from
        birthDate   → not in CIA data
        notes       → nothing meaningful to add for CIA

    Args:
        leader:       dict with name, title, honorific
        country:      country name string e.g. "Philippines"
        country_code: ISO alpha-2 e.g. "ph"
        slug:         URL slug e.g. "philippines"

    Returns:
        tuple (person_id, person_dict) or (None, None) if should skip
    """
    raw_name  = leader.get("name", "").strip()
    raw_title = leader.get("title", "").strip()
    honorific = leader.get("honorific", "").strip()

    # Skip VACANT and empty — 31 and 30 found in profiler
    if not raw_name or "VACANT" in raw_name.upper():
        print(f"  [SKIP] VACANT/empty — title: '{raw_title}' in {country}")
        return None, None

    # Split name using country-aware strategy
    name_parts = split_cia_name(raw_name, country_code)

    first  = name_parts["first_name"]
    last   = name_parts["last_name"]
    suffix = name_parts["suffix"]
    alias  = name_parts["alias"]
    emb_honorific = name_parts["embedded_honorific"]

    # Build display name from components
    parts = []
    if first:
        parts.append(first)
    if last:
        parts.append(last)
    if suffix:
        parts.append(suffix)
    full_name = " ".join(parts) if parts else raw_name

    # Merge both sources of honorifics
    all_honorifics = []
    if honorific:
        all_honorifics.append(honorific)
    if emb_honorific and emb_honorific != honorific:
        all_honorifics.append(emb_honorific)

    expanded_title = expand_abbreviations(raw_title)

    # Deterministic ID — based on source + country + raw name
    # Using raw_name (not cleaned) keeps ID stable across pipeline runs
    person_id = make_id("cia", country, raw_name)

    person = {
        # ── FtM properties ────────────────────────────────────────────────────
        "schema": "Person",
        "id": person_id,
        "properties": {
            # Thing (inherited)
            "name":        [full_name],
            "notes":       [],
            "retrievedAt": [RETRIEVED_AT],

            # Entity (inherited)
            "sourceUrl": [f"{CIA_BASE_URL}/{slug}/"],

            # Person
            "firstName":   [first]           if first           else [],
            "lastName":    [last]            if last            else [],
            "nameSuffix":  [suffix]          if suffix          else [],
            "alias":       [alias]           if alias           else [],
            "title":       all_honorifics,
            "position":    [expanded_title]  if expanded_title  else [],
            "topics":      ["role.pep"],
            # nationality intentionally omitted — see module docstring
        },

        # ── Custom metadata (outside FtM properties) ──────────────────────────
        # Used in dedup step to match persons across sources
        "sources":      ["cia_world_leaders"],
        "country":      country,
        "country_code": country_code,
        "date_updated": leader.get("date_updated", ""),
    }

    return person_id, person


def build_occupancy(leader, person_id, country):
    """
    Builds a FtM Occupancy entity linking a Person to their role.

    One Occupancy per position held. Same person with two positions
    gets two Occupancy entities both pointing to the same Person ID.
    This is how the 476 duplicate names from profiler are handled.

    FtM PROPERTIES USED:
        holder       (Occupancy) → Person entity ID
        role         (Occupancy) → expanded job title
        organization (Occupancy) → country name
        retrievedAt  (Thing)     → pipeline run date

    NOT SET (not in CIA data):
        startDate, endDate, status

    Args:
        leader:    dict with name and title
        person_id: ID of the Person this Occupancy belongs to
        country:   country name string

    Returns:
        occupancy dict
    """
    raw_title      = leader.get("title", "").strip()
    expanded_title = expand_abbreviations(raw_title)

    # Occupancy ID includes title so same person with two roles = two IDs
    occ_id = make_id("cia", country, leader.get("name", ""), raw_title)

    return {
        # ── FtM properties ────────────────────────────────────────────────────
        "schema": "Occupancy",
        "id": occ_id,
        "properties": {
            # Thing (inherited)
            "name":        [],
            "notes":       [],
            "retrievedAt": [RETRIEVED_AT],

            # Occupancy
            "holder":       [person_id],
            "role":         [expanded_title] if expanded_title else [],
            "organization": [country],
            "startDate":    [],
            "endDate":      [],
            "status":       [],
        },

        # ── Custom metadata ───────────────────────────────────────────────────
        "sources": ["cia_world_leaders"],
    }


# ─── Country Normalizer ───────────────────────────────────────────────────────

def normalize_country(country_data):
    """
    Normalizes one CIA country record into Person and Occupancy entities.

    DUPLICATE NAME HANDLING:
        Profiler found 476 cases where same name appears multiple times
        in one country — same person holds multiple positions.
        We track person_id per country. If already seen → add Occupancy only.
        Result: one Person entity, multiple Occupancy entities per position.

    Args:
        country_data: one country dict from cia_raw.json

    Returns:
        tuple (persons list, occupancies list)
    """
    country      = country_data["country"]
    raw_code     = country_data.get("country_code", "")
    leaders      = country_data.get("leaders", [])

    # Convert CIA's own country code to ISO for naming strategy detection
    # CIA uses non-standard codes like "RP" for Philippines
    # We need ISO alpha-2 like "ph" for ROMANCE_COUNTRIES / EAST_ASIAN_COUNTRIES
    country_code = to_iso_country(country)

    # Build URL slug from country name
    # e.g. "Philippines" → "philippines"
    # e.g. "Korea, North" → "korea-north"
    slug = (
        country.lower()
        .replace(", ", "-")
        .replace(" ", "-")
        .replace(".", "")
        .replace("'", "")
    )

    persons     = {}   # person_id → person dict (deduped by ID)
    occupancies = []

    for leader in leaders:
        person_id, person = build_person(leader, country, country_code, slug)

        if person_id is None:
            continue  # VACANT or empty — already logged

        # Add Person only once even if they hold multiple positions
        if person_id not in persons:
            persons[person_id] = person

        # Always add Occupancy — multiple roles = multiple occupancies
        occ = build_occupancy(leader, person_id, country)
        occupancies.append(occ)

    return list(persons.values()), occupancies


# ─── Run Function ─────────────────────────────────────────────────────────────

def run():
    """
    Loads cia_raw.json, normalizes all records, saves cia_normalized.json.

    OUTPUT STRUCTURE:
    {
        "meta": {
            "total_countries": 199,
            "total_persons": ...,
            "total_occupancies": ...,
            "skipped": ...,
            "failed_countries": [],
            "retrieved_at": "2026-05-29"
        },
        "persons": [...],
        "occupancies": [...]
    }

    Returns dict so FastAPI pipeline can use data without reading from disk.
    """
    start = time.time()
    elapsed = time.time() - start

    print(f"Loading {CIA_INPUT_PATH}...")
    with open(CIA_INPUT_PATH) as f:
        cia_data = json.load(f)
    print(f"  Loaded {len(cia_data)} countries\n")

    all_persons     = []
    all_occupancies = []
    total_skipped   = 0
    failed          = []

    for i, country_data in enumerate(cia_data, start=1):
        country = country_data.get("country", "?")
        print(f"  [{i}/{len(cia_data)}] {country}")

        try:
            persons, occupancies = normalize_country(country_data)
            all_persons.extend(persons)
            all_occupancies.extend(occupancies)

            total_leaders = len(country_data.get("leaders", []))
            skipped = total_leaders - len(occupancies)
            total_skipped += skipped

            print(f"    {len(persons)} persons, {len(occupancies)} occupancies"
                  + (f", {skipped} skipped" if skipped else ""))

        except Exception as e:
            failed.append(country)
            print(f"  [FAIL] {country}: {e}")

    output = {
        "meta": {
            "total_countries":   len(cia_data),
            "total_persons":     len(all_persons),
            "total_occupancies": len(all_occupancies),
            "skipped":           total_skipped,
            "failed_countries":  failed,
            "retrieved_at":      RETRIEVED_AT,
        },
        "persons":     all_persons,
        "occupancies": all_occupancies,
    }

    with open(CIA_OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    # ── Detailed summary report ───────────────────────────────────────────────
    print(f"\n{'═' * 55}")
    print(f"  CIA NORMALIZATION REPORT")
    print(f"{'═' * 55}")
    print(f"  Countries processed : {len(cia_data)}")
    print(f"  Countries failed    : {len(failed)}")
    print(f"  Persons created     : {len(all_persons)}")
    print(f"  Occupancies created : {len(all_occupancies)}")
    print(f"  Records skipped     : {total_skipped}")
    print(f"  Retrieved at        : {RETRIEVED_AT}")
    print(f"  Completed in        : {int(elapsed//60)}m {int(elapsed%60)}s")

    # ── Warning summary ───────────────────────────────────────────────────────
    # Count warnings from output for quick review
    persons_no_first   = sum(1 for p in all_persons if not p["properties"]["firstName"])
    persons_no_last    = sum(1 for p in all_persons if not p["properties"]["lastName"])
    persons_no_title   = sum(1 for p in all_persons if not p["properties"]["title"])
    persons_with_alias = sum(1 for p in all_persons if p["properties"]["alias"])
    persons_with_suffix = sum(1 for p in all_persons if p["properties"]["nameSuffix"])
    multi_occ_persons  = sum(
        1 for pid in set(
            occ["properties"]["holder"][0]
            for occ in all_occupancies
            if occ["properties"]["holder"]
        )
        if sum(
            1 for occ in all_occupancies
            if occ["properties"]["holder"]
            and occ["properties"]["holder"][0] == pid
        ) > 1
    )

    print(f"\n{'─' * 55}")
    print(f"  QUALITY CHECKS")
    print(f"{'─' * 55}")
    print(f"  Persons with no firstName    : {persons_no_first}")
    print(f"  Persons with no lastName     : {persons_no_last}")
    print(f"  Persons with no title/honor  : {persons_no_title}")
    print(f"  Persons with alias extracted : {persons_with_alias}")
    print(f"  Persons with suffix (Jr etc) : {persons_with_suffix}")
    print(f"  Persons with multiple roles  : {multi_occ_persons}")

    if failed:
        print(f"\n{'─' * 55}")
        print(f"  FAILED COUNTRIES")
        print(f"{'─' * 55}")
        for c in failed:
            print(f"    {c}")

    print(f"{'═' * 55}\n")

    return output


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()