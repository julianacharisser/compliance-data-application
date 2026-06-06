"""
db/loader.py
────────────
Loads all normalized and merged entities into the SQLite database.

INPUT FILES:
    dedup/merged.json        — 182 merged CIA+OFAC persons
    dedup/unmerged_cia.json  — CIA-only persons
    dedup/unmerged_ofac.json — OFAC-only persons
    transform/cia_normalized.json — occupancies

LOAD ORDER (important):
    1. Persons first — occupancies reference persons via foreign key
    2. Occupancies second
    3. FTS index last — populated after all persons are inserted

IDEMPOTENCY:
    Uses INSERT OR REPLACE — safe to run multiple times.
    Same id = row is updated, not duplicated.

TRANSACTION:
    All inserts wrapped in a single transaction.
    If anything fails, the whole load rolls back — no partial data.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from schema import get_connection, create_tables

# ─── Paths ────────────────────────────────────────────────────────────────────

MERGED_PATH        = "dedup/merged.json"
UNMERGED_CIA_PATH  = "dedup/unmerged_cia.json"
UNMERGED_OFAC_PATH = "dedup/unmerged_ofac.json"
CIA_NORMALIZED_PATH = "transform/cia_normalized.json"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_prop(person: dict, field: str, first_only: bool = False):
    """
    Safely gets a property from a person entity's properties dict.

    Args:
        person:     Person entity dict
        field:      FtM property name e.g. "name", "birthDate"
        first_only: if True, return first value as string; else return JSON list

    Returns:
        str (first value) or JSON string (full list) or None
    """
    values = person.get("properties", {}).get(field) or []
    if first_only:
        return values[0] if values else None
    return json.dumps(values)


def person_to_row(person: dict) -> tuple:
    """
    Converts a Person entity dict into a flat tuple for SQLite insertion.
    Maps FtM properties to table columns.

    Returns tuple matching the INSERT column order in insert_persons().
    """
    props = person.get("properties", {})

    # Primary name for search — first value in name list
    name = (props.get("name") or [""])[0]

    # Denormalized fields for fast filtering
    first_name   = (props.get("firstName") or [""])[0]
    last_name    = (props.get("lastName") or [""])[0]
    country      = person.get("country", "")
    country_code = person.get("country_code", "")
    topics       = json.dumps(props.get("topics") or [])
    sources      = json.dumps(person.get("sources") or [])

    # Full properties blob — everything the API needs for a full response
    properties   = json.dumps(props, ensure_ascii=False)

    # Dedup metadata
    merged_from  = json.dumps(person.get("merged_from")) if person.get("merged_from") else None
    ofac_uid     = person.get("ofac_uid", "")

    # Temporal
    date_updated = person.get("date_updated", "")
    retrieved_at = (props.get("retrievedAt") or [""])[0]

    return (
        person["id"],
        person.get("schema", "Person"),
        name,
        first_name,
        last_name,
        country,
        country_code,
        topics,
        sources,
        properties,
        merged_from,
        ofac_uid,
        date_updated,
        retrieved_at,
    )


def occupancy_to_row(occ: dict) -> tuple:
    """
    Converts an Occupancy entity dict into a flat tuple for SQLite insertion.
    """
    props      = occ.get("properties", {})
    holder_id  = (props.get("holder") or [""])[0]
    role       = (props.get("role") or [""])[0]
    country    = (props.get("organization") or [""])[0]
    sources    = json.dumps(occ.get("sources") or [])
    properties = json.dumps(props, ensure_ascii=False)

    return (
        occ["id"],
        holder_id,
        role,
        country,
        sources,
        properties,
    )


# ─── Loaders ──────────────────────────────────────────────────────────────────

def insert_persons(cursor: sqlite3.Cursor, persons: list[dict], label: str) -> int:
    """
    Inserts a list of Person entities into the persons table.
    Uses INSERT OR REPLACE for idempotency.

    Args:
        cursor:  SQLite cursor (inside an open transaction)
        persons: list of Person entity dicts
        label:   label for progress logging e.g. "merged", "CIA-only"

    Returns:
        Number of rows inserted/replaced
    """
    sql = """
        INSERT OR REPLACE INTO persons (
            id, schema, name, first_name, last_name,
            country, country_code, topics, sources,
            properties, merged_from, ofac_uid,
            date_updated, retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = [person_to_row(p) for p in persons]
    cursor.executemany(sql, rows)
    print(f"  Inserted {len(rows):>6} persons  [{label}]")
    return len(rows)


def insert_occupancies(cursor: sqlite3.Cursor, occupancies: list[dict]) -> int:
    """
    Inserts a list of Occupancy entities into the occupancies table.
    Only inserts if the holder_id exists in persons (foreign key).

    Args:
        cursor:      SQLite cursor (inside an open transaction)
        occupancies: list of Occupancy entity dicts

    Returns:
        Number of rows inserted/replaced
    """
    sql = """
        INSERT OR REPLACE INTO occupancies (
            id, holder_id, role, country, sources, properties
        ) VALUES (?, ?, ?, ?, ?, ?)
    """
    rows = [occupancy_to_row(o) for o in occupancies]
    cursor.executemany(sql, rows)
    print(f"  Inserted {len(rows):>6} occupancies")
    return len(rows)


def populate_fts(cursor: sqlite3.Cursor) -> None:
    """
    Populates the FTS5 index from the persons table.
    Called after all persons are inserted.

    Uses Python to parse JSON properties — more reliable than
    SQLite json_extract() which breaks on edge cases like empty
    lists, special characters, and single-value arrays.

    FTS indexes three fields:
        name      — primary display name
        aliases   — all known aliases space-joined
        positions — all known positions space-joined
    """
    print("  Rebuilding FTS index...")

    # Clear existing FTS data first
    cursor.execute("DELETE FROM persons_fts")

    # Load all persons
    rows = cursor.execute(
        "SELECT id, name, properties FROM persons"
    ).fetchall()

    fts_rows = []
    for i, row in enumerate(rows):
        # Use dict() to access by column name safely
        row_dict = dict(row)
        person_id = row_dict["id"]
        name      = row_dict["name"] or ""

        # Parse JSON properties safely
        try:
            props = json.loads(row_dict["properties"]) if row_dict["properties"] else {}
        except json.JSONDecodeError:
            props = {}

        # Space-join all aliases for FTS
        # e.g. ["Ali", "AKA2"] → "Ali AKA2"
        aliases = " ".join(props.get("alias") or []) or ""

        # Space-join all positions for FTS
        # e.g. ["President", "Commander"] → "President Commander"
        positions = " ".join(props.get("position") or []) or ""

        # DEBUG — print first 3 rows only
        if i < 3:
            print(f"  [DEBUG] id={person_id[:8]}")
            print(f"  [DEBUG] name='{name}'")
            print(f"  [DEBUG] raw properties type={type(row_dict['properties'])}")
            print(f"  [DEBUG] props keys={list(props.keys())[:5]}")
            print(f"  [DEBUG] alias raw={props.get('alias')}")
            print(f"  [DEBUG] aliases joined='{aliases}'")
            print(f"  [DEBUG] positions joined='{positions}'")
            print()

        fts_rows.append((person_id, name, aliases, positions))

    # Bulk insert into FTS
    cursor.executemany(
        "INSERT INTO persons_fts (id, name, aliases, positions) VALUES (?, ?, ?, ?)",
        fts_rows
    )

    count = cursor.execute(
        "SELECT COUNT(*) FROM persons_fts"
    ).fetchone()[0]
    print(f"  FTS index populated: {count} entries")
    

# ─── Main Run Function ────────────────────────────────────────────────────────

def run(run_id: str = None) -> dict:
    """
    Loads all normalized data into the SQLite database.

    Load order:
        1. Merged persons (CIA + OFAC matches)
        2. Unmerged CIA persons
        3. Unmerged OFAC persons
        4. Occupancies (after all persons exist)
        5. FTS index

    Args:
        run_id: pipeline run ID for audit log (generated if not provided)

    Returns:
        dict with counts for pipeline_runs audit log
    """
    start = time.time()
    run_id = run_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    print("Loading input files...")

    with open(MERGED_PATH, encoding="utf-8") as f:
        merged = json.load(f)

    with open(UNMERGED_CIA_PATH, encoding="utf-8") as f:
        unmerged_cia = json.load(f)

    with open(UNMERGED_OFAC_PATH, encoding="utf-8") as f:
        unmerged_ofac = json.load(f)

    with open(CIA_NORMALIZED_PATH, encoding="utf-8") as f:
        cia_data = json.load(f)
    occupancies = cia_data.get("occupancies", [])

    print(f"  Merged persons:    {len(merged)}")
    print(f"  Unmerged CIA:      {len(unmerged_cia)}")
    print(f"  Unmerged OFAC:     {len(unmerged_ofac)}")
    print(f"  Occupancies:       {len(occupancies)}\n")

    conn = get_connection()

    # Ensure tables exist
    create_tables(conn)

    try:
        cursor = conn.cursor()

        # Record pipeline run start
        cursor.execute("""
            INSERT OR REPLACE INTO pipeline_runs
                (run_id, started_at, status)
            VALUES (?, ?, 'running')
        """, (run_id, started_at))

        print("Inserting persons...")

        # Insert all persons — order doesn't matter for persons themselves
        total_persons = 0
        total_persons += insert_persons(cursor, merged,        "merged")
        total_persons += insert_persons(cursor, unmerged_cia,  "CIA-only")
        total_persons += insert_persons(cursor, unmerged_ofac, "OFAC-only")

        print("\nInserting occupancies...")
        total_occupancies = insert_occupancies(cursor, occupancies)

        print("\nPopulating FTS index...")
        populate_fts(cursor)

        # Update pipeline run as success
        completed_at = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE pipeline_runs SET
                completed_at          = ?,
                status                = 'success',
                cia_records_ingested  = ?,
                ofac_records_ingested = ?,
                duplicates_found      = ?,
                entities_stored       = ?
            WHERE run_id = ?
        """, (
            completed_at,
            len(unmerged_cia) + len(merged),   # CIA records
            len(unmerged_ofac) + len(merged),  # OFAC records
            len(merged),                        # duplicates merged
            total_persons,                      # total entities in DB
            run_id,
        ))

        conn.commit()

    except Exception as e:
        conn.rollback()

        # Record failure in pipeline_runs
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pipeline_runs SET
                completed_at  = ?,
                status        = 'failed',
                error_message = ?
            WHERE run_id = ?
        """, (datetime.now(timezone.utc).isoformat(), str(e), run_id))
        conn.commit()

        raise  # re-raise so caller knows it failed

    finally:
        conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    print(f"\n{'═' * 50}")
    print(f"  DATABASE LOAD COMPLETE")
    print(f"{'═' * 50}")
    print(f"  Total persons:      {total_persons}")
    print(f"  Total occupancies:  {total_occupancies}")
    print(f"  Merged entities:    {len(merged)}")
    print(f"  Run ID:             {run_id}")
    print(f"  Completed in:       {int(elapsed // 60)}m {int(elapsed % 60)}s")
    print(f"{'═' * 50}")

    return {
        "run_id":                 run_id,
        "cia_records_ingested":   len(unmerged_cia) + len(merged),
        "ofac_records_ingested":  len(unmerged_ofac) + len(merged),
        "duplicates_found":       len(merged),
        "entities_stored":        total_persons,
    }


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
