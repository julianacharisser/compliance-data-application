"""
pipeline.py
───────────
Orchestrates the full data pipeline from ingestion to database load.

Called by:
    - app.py lifespan on first boot (synchronous, blocks startup)
    - POST /pipeline/trigger endpoint (runs in background thread)
    - python pipeline.py (manual run from terminal)

PIPELINE STEPS:
    1. CIA scraper     → ingest/cia_raw.json
    2. OFAC parser     → ingest/ofac_raw.json
    3. CIA normalizer  → transform/cia_normalized.json
    4. OFAC normalizer → transform/ofac_normalized.json
    5. Dedup matcher   → dedup/dedup_results.json
    6. Merger          → dedup/merged.json + unmerged files
    7. DB loader       → db/compliance.db

Each step is wrapped in try/except — if one step fails the pipeline
stops and records the error in pipeline_runs table.

TRADE-OFFS:
    - Synchronous sequential steps — simple but slow (~8-9 min total)
    - Each step writes to disk — allows resume from any step manually
    - Single pipeline_runs row per execution — tracks status end to end
"""

import sys
import uuid
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
# Add project root to path so imports work regardless of where pipeline.py
# is called from (app.py, terminal, background thread)
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ingest")) 
sys.path.insert(0, str(ROOT / "transform"))
sys.path.insert(0, str(ROOT / "dedup"))
sys.path.insert(0, str(ROOT / "db"))

# ── Imports ───────────────────────────────────────────────────────────────────
import cia_scraper 
import ofac_parser 
import cia_normalizer
import ofac_normalizer
import matcher
import merger
import loader
from schema import get_connection

logger = logging.getLogger(__name__)

# ── Pipeline Steps ────────────────────────────────────────────────────────────

STEPS = [
    ("CIA scraper",      lambda: cia_scraper.run(limit=None)),
    ("OFAC parser",      lambda: ofac_parser.run(skip_download=False)),
    ("CIA normalizer",   lambda: normalize_cia.run()),
    ("OFAC normalizer",  lambda: normalize_ofac.run()),
    ("Dedup matcher",    lambda: matcher.run()),
    ("Merger",           lambda: merger.run()),
    ("DB loader",        lambda: loader.run()),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _update_run(run_id: str, **kwargs) -> None:
    """
    Updates a pipeline_runs row in the database.
    Used to track progress and record errors.
    """
    conn = get_connection()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [run_id]
        conn.execute(
            f"UPDATE pipeline_runs SET {set_clause} WHERE run_id = ?",
            values
        )
        conn.commit()
    finally:
        conn.close()


def _insert_run(run_id: str, started_at: str) -> None:
    """
    Creates a new pipeline_runs row with status=running.
    """
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO pipeline_runs
                (run_id, started_at, status)
            VALUES (?, ?, 'running')
        """, (run_id, started_at))
        conn.commit()
    finally:
        conn.close()


# ── Main Run Function ─────────────────────────────────────────────────────────

def run(run_id: str = None) -> dict:
    """
    Runs the full pipeline from ingestion to database load.

    Args:
        run_id: UUID for this pipeline run. Generated if not provided.
                Pass explicitly when called from POST /pipeline/trigger
                so the endpoint can return it immediately.

    Returns:
        dict with run_id, status, and step results

    Side effects:
        - Writes/updates pipeline_runs row in DB
        - Overwrites all intermediate JSON files
        - Rebuilds compliance.db
    """
    run_id = run_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    logger.info(f"Pipeline starting — run_id={run_id}")
    print(f"\n{'=' * 55}")
    print(f"  PIPELINE RUN STARTING")
    print(f"  run_id: {run_id}")
    print(f"  started: {started_at}")
    print(f"{'=' * 55}\n")

    # Record run start in DB
    _insert_run(run_id, started_at)

    step_results = {}

    for step_name, step_fn in STEPS:
        print(f"  > {step_name}...")
        step_start = time.time()

        try:
            result = step_fn()
            step_elapsed = round(time.time() - step_start, 1)
            step_results[step_name] = {"status": "success", "elapsed": step_elapsed}
            print(f"  OK {step_name} — {step_elapsed}s\n")

        except Exception as e:
            step_elapsed = round(time.time() - step_start, 1)
            error_msg = f"{step_name} failed after {step_elapsed}s: {str(e)}"

            logger.error(f"Pipeline step failed: {error_msg}", exc_info=True)
            print(f"  FAILED {step_name}: {e}\n")

            step_results[step_name] = {"status": "failed", "error": str(e)}

            # Record failure and stop
            _update_run(
                run_id,
                completed_at=datetime.now(timezone.utc).isoformat(),
                status="failed",
                error_message=error_msg,
            )

            return {
                "run_id": run_id,
                "status": "failed",
                "error":  error_msg,
                "steps":  step_results,
            }

    # All steps succeeded
    elapsed = round(time.time() - start_time, 1)
    completed_at = datetime.now(timezone.utc).isoformat()

    _update_run(
        run_id,
        completed_at=completed_at,
        status="success",
        error_message=None,
    )

    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print(f"{'=' * 55}")
    print(f"  PIPELINE COMPLETE")
    print(f"  run_id:  {run_id}")
    print(f"  elapsed: {minutes}m {seconds}s")
    print(f"{'=' * 55}\n")

    logger.info(f"Pipeline complete — run_id={run_id} elapsed={elapsed}s")

    return {
        "run_id":  run_id,
        "status":  "success",
        "elapsed": elapsed,
        "steps":   step_results,
    }


def is_db_empty() -> bool:
    """
    Returns True if the persons table has no rows.
    Used by app.py lifespan to decide whether to run pipeline on boot.
    """
    try:
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        conn.close()
        return count == 0
    except Exception:
        # Table does not exist yet
        return True


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = run()
    print(f"Final status: {result['status']}")
