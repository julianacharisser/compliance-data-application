"""
db/queries.py
─────────────
Search and fetch functions for the FastAPI endpoints.

All functions take a sqlite3.Connection and return plain dicts or lists
so FastAPI can serialize them directly to JSON.

FUNCTIONS:
    search_persons()    → GET /persons/search
    get_person_by_id()  → GET /persons/{id}
    get_stats()         → GET /stats
    get_pipeline_runs() → GET /pipeline/runs
    get_pipeline_run()  → GET /pipeline/runs/{run_id}

SEARCH STRATEGY:
    Two-phase search:
    1. FTS5 index for fast full-text matching across name + aliases + positions
    2. Column filters for country, source applied as WHERE clauses
    Falls back to LIKE search if FTS returns no results — handles edge cases
    where FTS tokenization misses short or special character queries.
"""

import json
import sqlite3
from typing import Optional

from schema import get_connection


# ─── Helpers ──────────────────────────────────────────────────────────────────

def row_to_dict(row: sqlite3.Row) -> dict:
    """
    Converts a sqlite3.Row to a plain dict.
    Parses JSON string fields back into Python objects.
    """
    d = dict(row)

    # Parse JSON fields back to Python objects
    for field in ("properties", "topics", "sources", "merged_from"):
        if d.get(field) and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass

    return d


def build_full_response(row: sqlite3.Row) -> dict:
    """
    Builds a full API response for a single person.
    Combines top-level columns with the full properties blob.
    """
    d = row_to_dict(row)
    return {
        "id":           d["id"],
        "schema":       d["schema"],
        "name":         d["name"],
        "country":      d["country"],
        "country_code": d["country_code"],
        "sources":      d["sources"],
        "topics":       d["topics"],
        "merged_from":  d["merged_from"],
        "properties":   d["properties"],
    }


# ─── Person Search ────────────────────────────────────────────────────────────

def search_persons(
    conn: sqlite3.Connection,
    q: str,
    country: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """
    Searches persons by name, alias, or position.

    Uses FTS5 for fast full-text search, then applies optional filters
    for country and source.

    Args:
        conn:    SQLite connection
        q:       search query e.g. "Putin", "minister iran"
        country: ISO alpha-2 country code filter e.g. "ru", "ir"
        source:  source filter — "cia", "ofac", or "all" (default)
        limit:   max results to return (default 20, max 100)
        offset:  pagination offset (default 0)

    Returns:
        dict with "results" list and "total" count
    """
    cursor = conn.cursor()
    limit = min(limit, 100)  # cap at 100

    # ── Phase 1: FTS search ───────────────────────────────────────────────────
    # FTS5 MATCH query — searches across name, aliases, positions
    # Simple prefix matching: putin* matches "putin", "putins" etc
    fts_query = " ".join(f'{word}*' for word in q.strip().split())

    try:
        fts_ids = cursor.execute("""
            SELECT id FROM persons_fts
            WHERE persons_fts MATCH ?
        """, (fts_query,)).fetchall()
        candidate_ids = [row[0] for row in fts_ids]
    except sqlite3.OperationalError:
        # FTS failed (e.g. special chars) — fall back to LIKE
        candidate_ids = []

    # ── Phase 2: LIKE fallback if FTS returned nothing ────────────────────────
    if not candidate_ids:
        like_pattern = f"%{q}%"
        fallback = cursor.execute("""
            SELECT id FROM persons
            WHERE name LIKE ?
               OR properties LIKE ?
        """, (like_pattern, like_pattern)).fetchall()
        candidate_ids = [row[0] for row in fallback]

    if not candidate_ids:
        return {"results": [], "total": 0}

    # ── Phase 3: Apply filters ────────────────────────────────────────────────
    # Build WHERE clause dynamically based on provided filters
    placeholders = ",".join("?" * len(candidate_ids))
    where_clauses = [f"id IN ({placeholders})"]
    params = list(candidate_ids)

    if country:
        where_clauses.append("country_code = ?")
        params.append(country.lower())

    if source and source != "all":
        # Sources stored as JSON array — use LIKE for simple matching
        source_map = {
            "cia":  "cia_world_leaders",
            "ofac": "ofac_sdn",
        }
        source_value = source_map.get(source.lower(), source)
        where_clauses.append("sources LIKE ?")
        params.append(f"%{source_value}%")

    where_sql = " AND ".join(where_clauses)

    # Get total count for pagination
    total = cursor.execute(
        f"SELECT COUNT(*) FROM persons WHERE {where_sql}",
        params
    ).fetchone()[0]

    # Get paginated results
    rows = cursor.execute(
        f"""
        SELECT id, schema, name, first_name, last_name,
               country, country_code, topics, sources,
               properties, merged_from, ofac_uid
        FROM persons
        WHERE {where_sql}
        ORDER BY name
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset]
    ).fetchall()

    results = [build_full_response(row) for row in rows]
    return {"results": results, "total": total}


# ─── Person by ID ─────────────────────────────────────────────────────────────

def get_person_by_id(conn: sqlite3.Connection, person_id: str) -> Optional[dict]:
    """
    Fetches a single person by ID with their occupancies.

    Returns:
        Full person dict with occupancies list, or None if not found
    """
    cursor = conn.cursor()

    row = cursor.execute("""
        SELECT id, schema, name, first_name, last_name,
               country, country_code, topics, sources,
               properties, merged_from, ofac_uid,
               date_updated, retrieved_at
        FROM persons
        WHERE id = ?
    """, (person_id,)).fetchone()

    if not row:
        return None

    person = build_full_response(row)

    # Fetch occupancies for this person
    occ_rows = cursor.execute("""
        SELECT id, role, country, sources, properties
        FROM occupancies
        WHERE holder_id = ?
        ORDER BY role
    """, (person_id,)).fetchall()

    person["occupancies"] = [
        {
            "id":         occ["id"],
            "role":       occ["role"],
            "country":    occ["country"],
            "properties": json.loads(occ["properties"]) if occ["properties"] else {},
        }
        for occ in occ_rows
    ]

    return person


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_stats(conn: sqlite3.Connection) -> dict:
    """
    Returns database statistics for GET /stats endpoint.

    Counts:
        - Total persons
        - CIA-only persons (role.pep but not sanction)
        - OFAC-only persons (sanction but not role.pep)
        - Merged persons (both sources)
        - Total occupancies
        - Last pipeline run info
    """
    cursor = conn.cursor()

    total_persons = cursor.execute(
        "SELECT COUNT(*) FROM persons"
    ).fetchone()[0]

    cia_only = cursor.execute(
        "SELECT COUNT(*) FROM persons WHERE sources = ?",
        (json.dumps(["cia_world_leaders"]),)
    ).fetchone()[0]

    ofac_only = cursor.execute(
        "SELECT COUNT(*) FROM persons WHERE sources = ?",
        (json.dumps(["ofac_sdn"]),)
    ).fetchone()[0]

    merged = cursor.execute(
        "SELECT COUNT(*) FROM persons WHERE merged_from IS NOT NULL"
    ).fetchone()[0]

    total_occupancies = cursor.execute(
        "SELECT COUNT(*) FROM occupancies"
    ).fetchone()[0]

    # Last successful pipeline run
    last_run = cursor.execute("""
        SELECT run_id, completed_at, entities_stored, duplicates_found
        FROM pipeline_runs
        WHERE status = 'success'
        ORDER BY completed_at DESC
        LIMIT 1
    """).fetchone()

    return {
        "total_persons":      total_persons,
        "cia_only":           cia_only,
        "ofac_only":          ofac_only,
        "merged":             merged,
        "total_occupancies":  total_occupancies,
        "last_pipeline_run":  dict(last_run) if last_run else None,
    }


# ─── Pipeline Runs ────────────────────────────────────────────────────────────

def get_pipeline_runs(
    conn: sqlite3.Connection,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """
    Returns paginated list of pipeline runs for GET /pipeline/runs.
    Most recent first.
    """
    cursor = conn.cursor()

    total = cursor.execute(
        "SELECT COUNT(*) FROM pipeline_runs"
    ).fetchone()[0]

    rows = cursor.execute("""
        SELECT run_id, started_at, completed_at, status,
               cia_records_ingested, ofac_records_ingested,
               duplicates_found, entities_stored, error_message
        FROM pipeline_runs
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()

    return {
        "results": [dict(row) for row in rows],
        "total":   total,
    }


def get_pipeline_run(
    conn: sqlite3.Connection,
    run_id: str,
) -> Optional[dict]:
    """
    Returns a single pipeline run by ID for GET /pipeline/runs/{run_id}.
    Returns None if not found.
    """
    cursor = conn.cursor()
    row = cursor.execute("""
        SELECT run_id, started_at, completed_at, status,
               cia_records_ingested, ofac_records_ingested,
               duplicates_found, entities_stored, error_message
        FROM pipeline_runs
        WHERE run_id = ?
    """, (run_id,)).fetchone()

    return dict(row) if row else None


# ─── Quick Test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = get_connection()

    print("Testing search_persons('putin')...")
    results = search_persons(conn, "putin")
    print(f"  Found: {results['total']}")
    if results["results"]:
        print(f"  First: {results['results'][0]['name']}")

    print("\nTesting get_stats()...")
    stats = get_stats(conn)
    for k, v in stats.items():
        print(f"  {k}: {v}")

    conn.close()
