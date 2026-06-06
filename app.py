"""
app.py
──────
FastAPI application — single entry point for the compliance screening system.

Run with:
    uvicorn app:app --reload

Then open:
    http://localhost:8000/docs   ← Swagger UI, test all endpoints here

STARTUP SEQUENCE (lifespan):
    1. Create DB tables if not exist
    2. If DB empty → run full pipeline (blocks ~8-9 min on first boot)
    3. Start APScheduler (daily at 2am UTC by default)
    4. Serve API requests

TRADE-OFFS DOCUMENTED:
    - Synchronous first-boot pipeline: simple, no race condition where
      someone searches before data exists. Downside: slow first start.
    - FTS5 with LIKE fallback: fast search with graceful degradation.
    - asyncio.to_thread() for trigger: keeps event loop free during pipeline.
    - limit/offset pagination: simple SQL-native. Cursor-based would be
      more consistent but overkill for 13k records.
    - 409 Conflict for duplicate trigger: prevents concurrent pipeline
      runs. Small race window between check and insert — acceptable.
"""

import sys
import uuid
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "db"))

import pipeline
import queries
from schema import get_connection, create_tables

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = ROOT / "config.yaml"

def load_config() -> dict:
    """Loads config.yaml. Returns defaults if file not found."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    logger.warning("config.yaml not found — using defaults")
    return {
        "database":  {"path": "db/compliance.db"},
        "scheduler": {"hour": 2, "minute": 0, "timezone": "UTC"},
        "search":    {"default_limit": 20, "max_limit": 100},
    }

config = load_config()

# ── Scheduler ─────────────────────────────────────────────────────────────────
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BackgroundScheduler(timezone=config["scheduler"].get("timezone", "UTC"))


def scheduled_pipeline_run():
    """
    Called by APScheduler on the configured schedule.
    Runs the full pipeline and logs the result.
    Errors are caught so the scheduler keeps running even if a run fails.
    """
    run_id = str(uuid.uuid4())
    logger.info(f"Scheduled pipeline run starting — run_id={run_id}")
    try:
        result = pipeline.run(run_id=run_id)
        logger.info(f"Scheduled pipeline run complete — status={result['status']}")
    except Exception as e:
        logger.error(f"Scheduled pipeline run failed: {e}", exc_info=True)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup and shutdown.

    Startup:
        1. Create DB tables if not exist
        2. If DB empty → run full pipeline synchronously
           TRADE-OFF: blocks startup for ~8-9 min on first boot.
           Simple and safe — no requests served until data is ready.
           Production alternative: run async, return 503 until ready.
        3. Start background scheduler

    Shutdown:
        - Stop scheduler cleanly
    """
    logger.info("App starting up...")

    # Step 1 — ensure tables exist
    conn = get_connection()
    create_tables(conn)
    conn.close()
    logger.info("Database tables ready")

    # Step 2 — first boot data load
    if pipeline.is_db_empty():
        logger.info("Database is empty — running initial pipeline (this will take ~8-9 minutes)")
        print("\n" + "=" * 55)
        print("  FIRST BOOT: Running initial pipeline...")
        print("  This will take approximately 8-9 minutes.")
        print("  The server will be ready when this completes.")
        print("=" * 55 + "\n")
        pipeline.run()
        logger.info("Initial pipeline complete")
    else:
        logger.info("Database already populated — skipping initial pipeline")

    # Step 3 — start scheduler
    sched_config = config.get("scheduler", {})
    scheduler.add_job(
        scheduled_pipeline_run,
        CronTrigger(
            hour=sched_config.get("hour", 2),
            minute=sched_config.get("minute", 0),
        ),
        id="daily_pipeline",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        f"Scheduler started — daily pipeline at "
        f"{sched_config.get('hour', 2):02d}:{sched_config.get('minute', 0):02d} UTC"
    )

    yield  # app runs here

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped. App shutdown complete.")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Compliance Screening API",
    description=(
        "Screens individuals against CIA World Leaders and OFAC SDN sanctions lists. "
        "Data is refreshed daily via automated pipeline."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Pydantic Response Models ──────────────────────────────────────────────────

class OccupancyOut(BaseModel):
    """A government position held by a person."""
    id:         str
    role:       Optional[str] = None
    country:    Optional[str] = None
    properties: dict = Field(default_factory=dict)


class PersonOut(BaseModel):
    """
    A Person entity from CIA World Leaders, OFAC SDN, or both (merged).
    topics: ["role.pep"] = politically exposed person (CIA)
            ["sanction"] = sanctioned individual (OFAC)
            ["role.pep", "sanction"] = appears in both (merged)
    """
    id:          str
    schema_type: str = Field(alias="schema", default="Person")
    name:        Optional[str] = None
    country:     Optional[str] = None
    country_code: Optional[str] = None
    sources:     list[str] = Field(default_factory=list)
    topics:      list[str] = Field(default_factory=list)
    merged_from: Optional[list[str]] = None
    properties:  dict = Field(default_factory=dict)
    occupancies: list[OccupancyOut] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class SearchResponse(BaseModel):
    """Paginated search results."""
    data:   list[PersonOut]
    total:  int
    limit:  int
    offset: int


class PipelineRunOut(BaseModel):
    """Record of a single pipeline execution."""
    run_id:                str
    started_at:            str
    completed_at:          Optional[str] = None
    status:                str
    cia_records_ingested:  Optional[int] = None
    ofac_records_ingested: Optional[int] = None
    duplicates_found:      Optional[int] = None
    entities_stored:       Optional[int] = None
    error_message:         Optional[str] = None


class TriggerResponse(BaseModel):
    """Response from POST /pipeline/trigger."""
    run_id:  str
    status:  str
    message: str


class StatsResponse(BaseModel):
    """Database statistics."""
    total_persons:      int
    cia_only:           int
    ofac_only:          int
    merged:             int
    total_occupancies:  int
    top_countries:      list[dict]
    last_pipeline_run:  Optional[dict] = None


class HealthResponse(BaseModel):
    """App health status."""
    status:              str
    database:            str
    scheduler:           str
    last_pipeline_run:   Optional[str] = None
    next_scheduled_run:  Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def get_db():
    """Returns a DB connection. Caller is responsible for closing it."""
    return get_connection()


def format_person(raw: dict) -> PersonOut:
    """
    Converts a raw dict from queries.py into a PersonOut Pydantic model.
    Handles missing fields gracefully.
    """
    occupancies = [
        OccupancyOut(
            id=o.get("id", ""),
            role=o.get("role"),
            country=o.get("country"),
            properties=o.get("properties", {}),
        )
        for o in raw.get("occupancies", [])
    ]

    return PersonOut(
        id=raw["id"],
        schema="Person",
        name=raw.get("name"),
        country=raw.get("country"),
        country_code=raw.get("country_code"),
        sources=raw.get("sources") or [],
        topics=raw.get("topics") or [],
        merged_from=raw.get("merged_from"),
        properties=raw.get("properties") or {},
        occupancies=occupancies,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get(
    "/persons/search",
    response_model=SearchResponse,
    summary="Search persons by name",
    description=(
        "Search for persons across CIA World Leaders and OFAC SDN list. "
        "Uses FTS5 full-text search with LIKE fallback. "
        "Filter by country (ISO alpha-2) or source."
    ),
    tags=["Persons"],
)
def search_persons(
    q: str = Query(..., min_length=1, description="Search query — name or partial name"),
    country: Optional[str] = Query(None, description="ISO alpha-2 country code e.g. 'ru', 'ir'"),
    source: Optional[str] = Query(None, description="Filter by source: 'cia', 'ofac', or 'all'"),
    limit: int = Query(20, ge=1, le=100, description="Max results (1-100)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """
    Core screening endpoint. Search for a person by name.

    Examples:
    - /persons/search?q=putin
    - /persons/search?q=marcos&country=ph
    - /persons/search?q=minister&source=cia&limit=50
    """
    conn = get_db()
    try:
        result = queries.search_persons(
            conn, q=q, country=country, source=source,
            limit=limit, offset=offset
        )
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
    finally:
        conn.close()

    persons = [format_person(p) for p in result["results"]]

    return SearchResponse(
        data=persons,
        total=result["total"],
        limit=limit,
        offset=offset,
    )


@app.get(
    "/persons/{entity_id}",
    response_model=PersonOut,
    summary="Get person by ID",
    description="Retrieve a single person by entity ID with all properties and occupancies.",
    tags=["Persons"],
)
def get_person(entity_id: str):
    """
    Retrieve a single person by their entity ID.
    Includes all FtM properties, linked occupancies, and dedup metadata.
    """
    conn = get_db()
    try:
        person = queries.get_person_by_id(conn, entity_id)
    except Exception as e:
        logger.error(f"Get person error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    if not person:
        raise HTTPException(status_code=404, detail=f"Person not found: {entity_id}")

    return format_person(person)


@app.get(
    "/pipeline/runs",
    response_model=list[PipelineRunOut],
    summary="List pipeline runs",
    description="List all pipeline executions with optional filters.",
    tags=["Pipeline"],
)
def list_pipeline_runs(
    status: Optional[str] = Query(None, description="Filter by status: 'success', 'failed', 'running'"),
    since: Optional[str] = Query(None, description="Filter runs after this ISO datetime e.g. '2026-01-01T00:00:00Z'"),
    limit: int = Query(10, ge=1, le=100, description="Max results"),
):
    """
    List pipeline runs. Most recent first.
    Use this to monitor pipeline health and history.
    """
    # Validate since parameter if provided
    if since:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid datetime format for 'since': {since}. Use ISO format e.g. '2026-01-01T00:00:00Z'"
            )

    conn = get_db()
    try:
        result = queries.get_pipeline_runs(conn, limit=limit, status=status, since=since)
    except Exception as e:
        logger.error(f"List pipeline runs error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    return result["results"]


@app.get(
    "/pipeline/runs/{run_id}",
    response_model=PipelineRunOut,
    summary="Get pipeline run by ID",
    description="Get details for a specific pipeline run.",
    tags=["Pipeline"],
)
def get_pipeline_run(run_id: str):
    """Get a single pipeline run by its run_id."""
    conn = get_db()
    try:
        run = queries.get_pipeline_run(conn, run_id)
    except Exception as e:
        logger.error(f"Get pipeline run error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    if not run:
        raise HTTPException(status_code=404, detail=f"Pipeline run not found: {run_id}")

    return run


@app.post(
    "/pipeline/trigger",
    response_model=TriggerResponse,
    status_code=202,
    summary="Manually trigger pipeline run",
    description=(
        "Starts a full pipeline run in the background. "
        "Returns immediately with a run_id to track progress. "
        "Poll GET /pipeline/runs/{run_id} to check status."
    ),
    tags=["Pipeline"],
)
async def trigger_pipeline():
    """
    Manually trigger a pipeline run.

    Returns 202 Accepted immediately — pipeline runs in background.
    Returns 409 Conflict if a pipeline is already running.

    TRADE-OFF: Small race window between checking for running pipeline
    and inserting new run row. Acceptable for assessment, production
    would use a distributed lock.
    """
    conn = get_db()
    try:
        # Check if pipeline already running
        running = conn.execute(
            "SELECT run_id FROM pipeline_runs WHERE status = 'running' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if running:
        raise HTTPException(
            status_code=409,
            detail=f"Pipeline already running — run_id: {running[0]}"
        )

    # Generate run_id now so we can return it immediately
    run_id = str(uuid.uuid4())

    # Run pipeline in background thread
    # asyncio.to_thread moves synchronous pipeline code to a thread pool
    # so the async event loop stays free to handle other requests
    # TRADE-OFF: SQLite connection must use check_same_thread=False
    asyncio.create_task(
        asyncio.to_thread(pipeline.run, run_id)
    )

    return TriggerResponse(
        run_id=run_id,
        status="running",
        message="Pipeline run started in background. Poll GET /pipeline/runs/{run_id} for status.",
    )


@app.get(
    "/stats",
    response_model=StatsResponse,
    summary="Database statistics",
    description="Summary statistics about the compliance database.",
    tags=["Stats & Health"],
)
def get_stats():
    """
    Returns counts and breakdowns of the compliance database.
    Includes top 20 countries by person count.
    """
    conn = get_db()
    try:
        base_stats = queries.get_stats(conn)

        # Top 20 countries by person count
        top_countries = conn.execute("""
            SELECT country, COUNT(*) as count
            FROM persons
            WHERE country != ''
            GROUP BY country
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()

        top_countries_list = [
            {"country": row[0], "count": row[1]}
            for row in top_countries
        ]

    except Exception as e:
        logger.error(f"Stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    return StatsResponse(
        total_persons=base_stats["total_persons"],
        cia_only=base_stats["cia_only"],
        ofac_only=base_stats["ofac_only"],
        merged=base_stats["merged"],
        total_occupancies=base_stats["total_occupancies"],
        top_countries=top_countries_list,
        last_pipeline_run=base_stats.get("last_pipeline_run"),
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Check if the app, database, and scheduler are running correctly.",
    tags=["Stats & Health"],
)
def health_check():
    """
    Health check endpoint.
    Always returns 200 — check the status fields for actual health.
    """
    # Check database
    db_status = "connected"
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
    except Exception as e:
        db_status = f"error: {str(e)}"

    # Check scheduler
    sched_status = "running" if scheduler.running else "stopped"

    # Last pipeline run
    last_run = None
    next_run = None
    try:
        conn = get_db()
        row = conn.execute("""
            SELECT completed_at FROM pipeline_runs
            WHERE status = 'success'
            ORDER BY completed_at DESC
            LIMIT 1
        """).fetchone()
        conn.close()
        if row:
            last_run = row[0]
    except Exception:
        pass

    # Next scheduled run
    try:
        job = scheduler.get_job("daily_pipeline")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    except Exception:
        pass

    overall = "ok" if db_status == "connected" and sched_status == "running" else "degraded"

    return HealthResponse(
        status=overall,
        database=db_status,
        scheduler=sched_status,
        last_pipeline_run=last_run,
        next_scheduled_run=next_run,
    )


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
