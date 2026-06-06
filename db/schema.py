"""
db/schema.py
────────────
Creates all SQLite tables for the compliance database.

Run this once before loading data:
    python db/schema.py

Tables:
    persons        — all Person entities from CIA, OFAC, and merged
    occupancies    — CIA position records linked to persons
    pipeline_runs  — audit log of every pipeline execution

DESIGN DECISIONS:
    - Hybrid storage: searchable fields as real columns, full FtM
      properties stored as JSON blob. This allows fast filtering
      on common fields while preserving all data for API responses.
    - TEXT for all IDs — UUIDs are strings, not integers.
    - INSERT OR REPLACE for idempotent loads — safe to re-run.
    - Foreign key on occupancies.holder_id — enforced at query time.
    - FTS5 virtual table on persons for full-text search across
      name, alias, position fields without LIKE '%query%' scans.
"""

import sqlite3
from pathlib import Path

DB_PATH = "db/compliance.db" 


def get_connection() -> sqlite3.Connection:
    """
    Returns a SQLite connection with foreign keys enabled.
    Used by schema.py, loader.py, and queries.py.
    """
    Path("db").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    # Return rows as dicts instead of tuples
    conn.row_factory = sqlite3.Row
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    """
    Creates all tables. Safe to run multiple times — uses IF NOT EXISTS.
    """
    cursor = conn.cursor()

    # ── persons ───────────────────────────────────────────────────────────────
    # Stores every Person entity from all three sources:
    #   - merged (CIA + OFAC match)
    #   - unmerged CIA (current leaders not on sanctions list)
    #   - unmerged OFAC (sanctioned individuals not in government)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS persons (
            id           TEXT PRIMARY KEY,
            schema       TEXT NOT NULL DEFAULT 'Person',

            -- Denormalized for fast search and filtering
            name         TEXT,        -- primary display name
            first_name   TEXT,
            last_name    TEXT,
            country      TEXT,        -- CIA country name e.g. "Philippines"
            country_code TEXT,        -- ISO alpha-2 e.g. "ph"
            topics       TEXT,        -- JSON list e.g. '["role.pep", "sanction"]'
            sources      TEXT,        -- JSON list e.g. '["cia_world_leaders"]'

            -- Full FtM properties as JSON blob
            -- Contains: alias, position, birthDate, nationality, citizenship,
            --           passportNumber, idNumber, address, notes, sourceUrl etc.
            properties   TEXT NOT NULL DEFAULT '{}',

            -- Dedup metadata
            merged_from  TEXT,        -- JSON list [cia_id, ofac_id] or NULL
            ofac_uid     TEXT,        -- OFAC uid for cross-referencing raw data

            -- Temporal metadata
            date_updated TEXT,        -- when CIA last updated this record
            retrieved_at TEXT         -- when we scraped it
        )
    """)

    # ── occupancies ───────────────────────────────────────────────────────────
    # CIA position records — one per person-role pair.
    # A person can have multiple occupancies (multiple government roles).
    # holder_id references persons.id — person must exist before occupancy.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS occupancies (
            id           TEXT PRIMARY KEY,
            holder_id    TEXT NOT NULL REFERENCES persons(id),
            role         TEXT,        -- expanded position title
            country      TEXT,        -- country this role is in
            sources      TEXT,        -- JSON list
            properties   TEXT NOT NULL DEFAULT '{}'  -- full FtM properties
        )
    """)

    # ── pipeline_runs ─────────────────────────────────────────────────────────
    # Audit log for every pipeline execution.
    # Powers GET /pipeline/runs and GET /pipeline/runs/{id} endpoints.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id                TEXT PRIMARY KEY,
            started_at            TEXT NOT NULL,
            completed_at          TEXT,
            status                TEXT NOT NULL DEFAULT 'running',
                                  -- 'running' | 'success' | 'failed'
            cia_records_ingested  INTEGER DEFAULT 0,
            ofac_records_ingested INTEGER DEFAULT 0,
            duplicates_found      INTEGER DEFAULT 0,
            entities_stored       INTEGER DEFAULT 0,
            error_message         TEXT    -- NULL on success
        )
    """)

    # ── FTS5 virtual table ────────────────────────────────────────────────────
    # Full-text search index over name, alias, and position fields.
    # Allows fast: SELECT * FROM persons_fts WHERE persons_fts MATCH 'putin'
    # Much faster than LIKE '%putin%' on large datasets.
    # content= links it to the persons table — updates automatically.
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS persons_fts
        USING fts5(
            id UNINDEXED,   -- not searchable, used to join back to persons
            name,
            aliases,        -- space-joined alias list for FTS
            positions,      -- space-joined position list for FTS
            tokenize='unicode61 remove_diacritics 2'
                            -- handles accented chars: é=e, ü=u etc.
        )
    """)

    # ── Indexes ───────────────────────────────────────────────────────────────
    # Index on country_code for fast country filtering in the API
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_persons_country_code
        ON persons(country_code)
    """)

    # Index on sources for fast source filtering (cia_world_leaders / ofac_sdn)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_persons_sources
        ON persons(sources)
    """)

    # Index on holder_id for fast occupancy lookups by person
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_occupancies_holder
        ON occupancies(holder_id)
    """)

    conn.commit()
    print("Tables created successfully")
    print(f"  persons, occupancies, pipeline_runs, persons_fts")
    print(f"  Indexes: country_code, sources, holder_id")


def drop_tables(conn: sqlite3.Connection) -> None:
    """
    Drops all tables. Use for a clean rebuild during development.
    WARNING: destroys all data.
    """
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS persons_fts")
    cursor.execute("DROP TABLE IF EXISTS occupancies")
    cursor.execute("DROP TABLE IF EXISTS persons")
    cursor.execute("DROP TABLE IF EXISTS pipeline_runs")
    conn.commit()
    print("All tables dropped")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = get_connection()
    create_tables(conn)
    conn.close()
    print(f"\nDatabase ready at {DB_PATH}")
