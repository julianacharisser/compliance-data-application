# Compliance Data Application

A compliance screening pipeline that ingests, normalises, and cross-references individuals from two public watchlists, then exposes them through a REST API.

## Data Sources

| Source | Records | Description |
|--------|---------|-------------|
| [CIA World Leaders](https://www.cia.gov/resources/world-leaders/) | ~5,273 persons | Current government officials across 199 countries |
| [OFAC SDN List](https://www.treasury.gov/ofac/downloads/sdn.xml) | ~7,506 persons | US Treasury sanctioned individuals |
| Merged (both sources) | ~134 persons | Persons appearing in both lists |

---

## Architecture

### Pipeline Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                            INGESTION                                 │
│                                                                      │
│   ┌─────────────────────────┐      ┌─────────────────────────────┐  │
│   │    cia_scraper.py       │      │      ofac_parser.py         │  │
│   │                         │      │                             │  │
│   │  Playwright browser     │      │  HTTP download + XML parse  │  │
│   │  Gatsby page-data.json  │      │  Namespace handling         │  │
│   │  199 country pages      │      │  sdn.xml (~30MB)            │  │
│   └────────────┬────────────┘      └──────────────┬──────────────┘  │
│                │                                  │                 │
│                ▼                                  ▼                 │
│   ┌─────────────────────────┐      ┌─────────────────────────────┐  │
│   │   ingest/cia_raw.json   │      │   ingest/ofac_raw.json      │  │
│   │   199 countries         │      │   7,506 individuals         │  │
│   │   5,802 leaders         │      │                             │  │
│   └────────────┬────────────┘      └──────────────┬──────────────┘  │
└────────────────┼──────────────────────────────────┼─────────────────┘
                 │                                  │
                 ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          NORMALISATION                              │
│                                                                     │
│   ┌─────────────────────────┐      ┌─────────────────────────────┐  │
│   │   normalize_cia.py      │      │    normalize_ofac.py        │  │
│   │                         │      │                             │  │
│   │  FtM Person entities    │      │  FtM Person entities        │  │
│   │  FtM Occupancy entities │      │  Aliases, DOBs, IDs         │  │
│   │  Abbreviation expansion │      │  Nationality / citizenship  │  │
│   │  topics: role.pep       │      │  topics: sanction           │  │
│   └────────────┬────────────┘      └──────────────┬──────────────┘  │
│                │                                  │                 │
│                ▼                                  ▼                 │
│   ┌─────────────────────────┐      ┌─────────────────────────────┐  │
│   │  cia_normalized.json    │      │  ofac_normalized.json       │  │
│   │  5,273 persons          │      │  7,506 persons              │  │
│   │  5,741 occupancies      │      │                             │  │
│   └────────────┬────────────┘      └──────────────┬──────────────┘  │
└────────────────┼──────────────────────────────────┼─────────────────┘
                 │                                  │
                 └──────────────┬───────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         DEDUPLICATION                               │
│                                                                     │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │                       matcher.py                             │  │
│   │                                                              │  │
│   │   Block by country  →  Compare name pairs  →  Score          │  │
│   │                                                              │  │
│   │   Name similarity   60%   (rapidfuzz token_sort_ratio)       │  │
│   │   Country match     30%   (CIA country vs OFAC nationality)  │  │
│   │   Position overlap  10%   (shared title tokens)              │  │
│   │                                                              │  │
│   │   ≥ 85 → AUTO_MERGE    65–84 → REVIEW    < 65 → SKIP         │  │
│   └──────────────────────────────────┬───────────────────────────┘  │
│                                      │                              │
│                                      ▼                              │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │                        merger.py                             │  │
│   │                                                              │  │
│   │   134 AUTO_MERGE pairs  →  canonical merged entities         │  │
│   │   CIA id kept  │  OFAC birthDate/nationality wins            │  │
│   │   topics: [role.pep, sanction]  │  sources: [cia, ofac]      │  │
│   └──────────┬─────────────┬────────────────────────────────── ──┘  │
│              │             │                                        │
│              ▼             ▼                                        │
│   merged.json    unmerged_cia.json    unmerged_ofac.json            │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           DATABASE                                  │
│                                                                     │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │                    db/loader.py                              │  │
│   │   INSERT OR REPLACE  (idempotent)  +  FTS5 index rebuild     │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│   ┌───────────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────┐   │
│   │   persons     │  │ occupancies  │  │ pipeline │  │ persons  │   │
│   │   12,654 rows │  │  5,741 rows  │  │  _runs   │  │  _fts    │   │
│   │   JSON blob   │  │  holder_id → │  │ audit log│  │ FTS5     │   │
│   │   + columns   │  │  persons.id  │  │          │  │ index    │   │
│   └───────────────┘  └──────────────┘  └──────────┘  └──────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          API  (app.py)                               │
│                                                                      │
│   GET  /persons/search      FTS5 search + country / source filter    │
│   GET  /persons/{id}        Full record with occupancies             │
│   POST /pipeline/trigger    Background run via asyncio.to_thread()   │
│   GET  /pipeline/runs       Audit log                                │
│   GET  /pipeline/runs/{id}  Single run detail                        │
│   GET  /stats               Aggregated counts + top countries        │
│   GET  /health              DB + APScheduler status                  │
│                                                                      │
│   APScheduler  ──  cron 02:00 UTC  ──►  pipeline.run()               │
│   Swagger UI   ──  http://localhost:8000/docs                        │
└──────────────────────────────────────────────────────────────────────┘
```

### Request Flow

```
Client
  │
  │  HTTP request
  ▼
Uvicorn (ASGI server)
  │
  │  routes request
  ▼
FastAPI (app.py)
  │  validates params via Pydantic
  │
  ▼
queries.py
  │  SQL + FTS5
  ▼
compliance.db  ──►  JSON response  ──►  Client
```

### Pipeline Trigger Flow

```
POST /pipeline/trigger
  │
  ├─► status = 'running' already?  ──►  409 Conflict
  │
  ├─► generate run_id
  ├─► INSERT pipeline_runs (status = running)
  ├─► asyncio.to_thread(pipeline.run, run_id)
  │
  └─► 202 Accepted  { run_id, status: "running" }
          │
          │  background thread
          ▼
      pipeline.run()  (~8-9 min)
          │
          └─► UPDATE pipeline_runs (status = success/failed)
```

---

## Setup

### Requirements

- Python 3.11+
- pip

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

### Run

```bash
uvicorn app:app --reload
```

On **first boot** the app runs the full pipeline (~8-9 minutes) before serving requests. All subsequent starts are immediate — data is already in the database.

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

## API Reference

### `GET /persons/search`

Search for persons by name across both lists.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | ✓ | Name or partial name |
| `country` | string | | ISO alpha-2 code e.g. `ru`, `ir`, `kp` |
| `source` | string | | `cia`, `ofac`, or `all` (default) |
| `limit` | int | | Max results, 1–100 (default 20) |
| `offset` | int | | Pagination offset (default 0) |

```bash
curl "http://localhost:8000/persons/search?q=putin"
curl "http://localhost:8000/persons/search?q=kim&country=kp&limit=10"
curl "http://localhost:8000/persons/search?q=minister&source=cia"
```

### `GET /persons/{id}`

Full person record including all FtM properties and occupancies.

```bash
curl "http://localhost:8000/persons/abc123"
```

### `POST /pipeline/trigger`

Starts a pipeline run in the background. Returns immediately.

```bash
curl -X POST "http://localhost:8000/pipeline/trigger"
# { "run_id": "...", "status": "running", "message": "..." }

# Poll for status
curl "http://localhost:8000/pipeline/runs/{run_id}"
```

### `GET /pipeline/runs`

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by `success`, `failed`, `running` |
| `since` | string | ISO datetime e.g. `2026-01-01T00:00:00Z` |
| `limit` | int | Max results (default 10) |

### `GET /stats`

Returns total persons by source, top 20 countries, occupancy count, last pipeline run.

### `GET /health`

```json
{
  "status": "ok",
  "database": "connected",
  "scheduler": "running",
  "last_pipeline_run": "2026-06-04T02:00:00Z",
  "next_scheduled_run": "2026-06-05T02:00:00Z"
}
```

---

## Data Model

Follows the [FollowTheMoney](https://followthemoney.tech/explorer/) schema.

### Person

```json
{
  "id": "abc123",
  "schema": "Person",
  "sources": ["cia_world_leaders"],
  "topics": ["role.pep"],
  "country": "Russia",
  "country_code": "ru",
  "properties": {
    "name": ["Vladimir Putin"],
    "firstName": ["Vladimir"],
    "lastName": ["Putin"],
    "position": ["President"],
    "nationality": ["ru"]
  }
}
```

**`topics` values:**

| Value | Meaning |
|-------|---------|
| `role.pep` | Politically Exposed Person (CIA) |
| `sanction` | Sanctioned individual (OFAC) |
| `role.pep` + `sanction` | Appears in both lists (merged) |

### Occupancy

```json
{
  "id": "def456",
  "schema": "Occupancy",
  "properties": {
    "holder": ["abc123"],
    "role": ["President"],
    "organization": ["Russia"]
  }
}
```

---

## Deduplication

### Method

Blocked fuzzy matching with weighted scoring — follows the methodology used by [OpenSanctions](https://www.opensanctions.org/).

```
For each CIA person:
  1. Find OFAC persons in the same country block
  2. Compare names using rapidfuzz (handles transliteration)
  3. Score: name 60% + country 30% + position 10%
  4. ≥ 85 → AUTO_MERGE  |  65–84 → REVIEW  |  < 65 → SKIP
```

### Results

| Method | Candidates | AUTO_MERGE | Time |
|--------|------------|------------|------|
| Fuzzy baseline | 2,518 | 82 | 1.04s |
| Weighted (no alias) | 4,209 | 84 | 3.02s |
| Weighted + aliases | 6,631 | 134 | 11.66s |

---

## Configuration

`config.yaml`:

```yaml
database:
  path: "db/compliance.db"

scheduler:
  hour: 2
  minute: 0
  timezone: "UTC"

search:
  default_limit: 20
  max_limit: 100

dedup:
  auto_merge_threshold: 85
  review_threshold: 65
```

---

## Project Structure

```
compliance_data_application/
│
├── app.py                  ← FastAPI entry point + lifespan
├── pipeline.py             ← Pipeline orchestrator
├── config.yaml             ← Settings
├── requirements.txt
├── responses.md            ← Written answers (Part 7)
│
├── ingest/
│   ├── cia_scraper.py      ← Playwright + Gatsby JSON API
│   └── ofac_parser.py      ← XML download + namespace parsing
│
├── transform/
│   ├── cia_normalizer.py    ← FtM Person + Occupancy entities
│   ├── ofac_normalizer.py   ← FtM Person entities
│   ├── helpers.py           ← Shared utilities
│   └── profiler.py          ← Data quality report
│
├── dedup/
│   ├── matcher.py                ← Weighted fuzzy matcher (main)
│   ├── matcher_fuzzy.py          ← Baseline fuzzy matcher
│   ├── fuzzy_matcher.py          ← Baseline fuzzy matcher
│   ├── weighted_matcher.py       ← Baseline weighted fuzzy matcher
│   └── merger.py                 ← Canonical entity merger
│
└── db/
    ├── schema.py           ← SQLite table definitions + FTS5
    ├── loader.py           ← INSERT OR REPLACE + FTS rebuild
    └── queries.py          ← Search and fetch helpers
```

---

## Known Limitations

- First boot takes ~8-9 minutes (initial pipeline run blocks startup)
- SQLite is single-writer — concurrent pipeline runs not supported
- 2,015 OFAC persons have no nationality — matched on name only
- Transliteration map covers common Arabic variants only
- Khamenei (CIA) vs Khamenei (OFAC) scores 65.6 — sits in REVIEW
- Korean name order affects token matching
- Removed persons are not deleted between pipeline runs (no hard delete)
- Scheduler timezone is UTC — adjust `config.yaml` for local time

---

## Sources

- CIA World Leaders — https://www.cia.gov/resources/world-leaders/
- OFAC SDN List — https://www.treasury.gov/ofac/downloads/sdn.xml
- FollowTheMoney schema — https://followthemoney.tech/explorer/
- OpenSanctions methodology — https://www.opensanctions.org/docs/enrichment/
