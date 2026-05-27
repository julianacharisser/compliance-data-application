# Compliance Data Pipeline & API — Take-Home Technical Assessment

**Position:** Junior Data Scientist / Data Engineer  
**Submission:** A GitHub repository containing all code, database, documentation, and a brief write-up.  
**Time:** Take as long as you need. We care about quality and thoughtfulness, not speed.

---

## Background & Scenario

You have been hired by a compliance technology startup building screening tools for financial institutions. Your task is to build a **self-contained compliance data application** that:

1. **Ingests** data about politically exposed persons (PEPs) and sanctioned individuals from two government sources
2. **Normalizes** it into a standard entity model
3. **Stores** it in a queryable database
4. **Exposes** it via a REST API for downstream screening systems
5. **Runs a background scheduler** for automatic data refresh

**Everything runs from a single command:**

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

That's it. When the application starts, it should boot the API, initialize the database, start the background scheduler, and be ready to serve requests. No separate scripts, no manual pipeline runs, no multi-step setup.

---

## System Architecture

```
                        uvicorn app:app
                              │
                              ▼
                    ┌─────────────────────┐
                    │     FastAPI App      │
                    │                     │
                    │  Startup Event:     │
                    │  • Init database    │
                    │  • Run pipeline     │  ◄── Runs on first boot if DB is empty
                    │  • Start scheduler  │  ◄── Background scheduler for refresh
                    │                     │
                    │  API Endpoints:     │
                    │  • Search persons   │
                    │  • Lookup by ID     │
                    │  • Pipeline logs    │
                    │  • Trigger refresh  │
                    │  • Stats & health   │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────┐
                    │  SQLite Database │
                    │                 │
                    │ • persons       │
                    │ • occupancies   │
                    │ • pipeline_runs │
                    └─────────────────┘
                             ▲
                             │
              ┌──────────────┴──────────────┐
              │       PIPELINE ENGINE       │
              │                             │
              │  ┌────────┐  ┌───────────┐  │
              │  │INGEST  │  │ TRANSFORM │  │
              │  │        │  │           │  │
              │  │CIA HTML │  │ FtM Map   │  │
              │  │OFAC XML │  │ Normalize │  │
              │  └────┬───┘  └─────┬─────┘  │
              │       │            │        │
              │       ▼            ▼        │
              │  ┌─────────────────────┐    │
              │  │    DEDUPLICATE      │    │
              │  │  Cross-source match │    │
              │  │  Merge duplicates   │    │
              │  └─────────────────────┘    │
              └─────────────────────────────┘
```

---

## Data Sources

### Source 1 — CIA World Leaders (Web Scraping)

The **CIA Chiefs of State and Cabinet Members of Foreign Governments** directory — a public-domain listing of world leaders and senior government officials, updated weekly.

- **Index Page:** `https://www.cia.gov/resources/world-leaders/foreign-governments/`
- **Example Country Page:** `https://www.cia.gov/resources/world-leaders/foreign-governments/iran`
- **About:** [https://www.cia.gov/resources/world-leaders/](https://www.cia.gov/resources/world-leaders/)

This source has **~199 country pages**, each rendered as HTML. There is **no downloadable bulk file** — you must scrape it.

Each country page contains:
- The country name
- A "Last Updated" date
- A list of government positions (rendered as headings) with the officeholder's name below each position
- Some pages include contextual notes (e.g., Libya's page has a paragraph about the interim government)

**Your scraper must:**
1. Discover all country page URLs from the index page (the index may be paginated or offer a "show all" option — inspect and handle accordingly)
2. Visit each country page and extract every **person–position pair**
3. Extract the country name and "Last Updated" date
4. Handle edge cases: "VACANT" positions, names with titles/ranks (e.g., "Gen.", "Dr."), introductory paragraphs mixed with position listings
5. Implement polite scraping: 1–2 second delays, descriptive User-Agent, retry with exponential backoff

> **Note:** The CIA website is a Gatsby-rendered static site. You'll need to inspect the actual page source to determine the best parsing strategy. If the HTML structure isn't what you expect, document what you find and adapt.

---

### Source 2 — OFAC SDN List (XML Download)

The **Specially Designated Nationals and Blocked Persons (SDN)** list from the U.S. Treasury's Office of Foreign Assets Control.

- **XML URL:** `https://www.treasury.gov/ofac/downloads/sdn.xml`
- **Schema (XSD):** `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML.xsd`
- **Documentation:** [https://ofac.treasury.gov/sanctions-list-service](https://ofac.treasury.gov/sanctions-list-service)

The SDN XML uses a namespace (updated May 2024):
`https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML`

> Focus only on records where `<sdnType>` is `"Individual"`.

The XML includes per individual: `<lastName>`, `<firstName>`, `<title>`, `<programList>`, `<akaList>`, `<addressList>`, `<nationalityList>`, `<citizenshipList>`, `<dateOfBirthList>`, `<idList>`.

---

### Why these two sources?

Government officials from **sanctioned countries** (Iran, North Korea, Syria, Russia, etc.) frequently appear on **both** lists — as current officials in the CIA directory and as sanctioned individuals on the OFAC SDN list. The same person may appear as "Hossein AMIR-ABDOLLAHIAN" (CIA) and "AMIRABDOLLAHIAN, Hossein" with alias "Amir Abdollahian" (OFAC). Finding these overlaps is the core deduplication challenge.

---

## Target Data Model: FollowTheMoney (FtM)

Normalize all data into the **FollowTheMoney** entity model.

**Documentation:** [https://followthemoney.tech/docs/](https://followthemoney.tech/docs/)  
**Model Explorer:** [https://followthemoney.tech/explorer/schemata/](https://followthemoney.tech/explorer/schemata/)

### 1. `Person` schema

Reference: [https://followthemoney.tech/explorer/schemata/Person/](https://followthemoney.tech/explorer/schemata/Person/)

```json
{
  "id": "<deterministic unique ID>",
  "schema": "Person",
  "properties": {
    "name": ["FULL NAME"],
    "alias": ["AKA 1", "AKA 2"],
    "firstName": ["GIVEN NAME"],
    "lastName": ["FAMILY NAME"],
    "nationality": ["xx"],
    "birthDate": ["YYYY-MM-DD"],
    "birthPlace": ["City, Country"],
    "position": ["Minister of Foreign Affairs"],
    "country": ["xx"]
  }
}
```

### 2. `Occupancy` schema

Reference: [https://followthemoney.tech/explorer/schemata/Occupancy/](https://followthemoney.tech/explorer/schemata/Occupancy/)

```json
{
  "id": "<deterministic unique ID>",
  "schema": "Occupancy",
  "properties": {
    "holder": ["<Person entity ID>"],
    "post": ["<Position title>"],
    "status": ["current"],
    "startDate": ["YYYY-MM-DD"],
    "country": ["xx"]
  }
}
```

**Key FtM rules:**
- All property values are **arrays of strings**
- Countries use **ISO 3166-1 alpha-2** codes
- Entity IDs must be **deterministic** (derived from source data, not random)

> **Important:** Verify exact property names against the Model Explorer. If you find discrepancies between this document and the live documentation, follow the documentation and note the difference. Reading schema docs is part of the job.

---

## Part 1 — Data Ingestion (25 points)

### 1A. Web Scraper: CIA World Leaders (15 points)

- Discover all ~199 country page URLs from the index
- Extract every person–position pair, country name, and last-updated date
- Handle edge cases: "VACANT" positions, titles/ranks, contextual paragraphs
- Polite scraping: delays, User-Agent, retry with backoff

### 1B. XML Parser: OFAC SDN List (10 points)

- Download and parse the XML with correct namespace handling
- Extract all Individual-type records with full details
- Handle: multiple aliases, multiple DOBs, missing fields

---

## Part 2 — Schema Mapping & Normalization (20 points)

- Map records to FtM `Person` and `Occupancy` entities
- Normalize names: handle "LAST, First" (OFAC) vs "First LAST" (CIA), separate titles/ranks
- Normalize dates to `YYYY-MM-DD`
- Convert country names to ISO alpha-2 codes ("RUSSIAN FEDERATION" → `"ru"`, "KOREA, NORTH" → `"kp"`)
- Expand CIA abbreviations ("Min." → "Minister", "Pres." → "President", "Dep." → "Deputy")
- Tag each entity with its source

---

## Part 3 — Cross-Source Deduplication (20 points)

- Identify candidate duplicates across sources using name similarity, country match, and position overlap
- Score pairs with a documented similarity metric
- Handle transliteration variants ("MUHAMMAD" / "MOHAMMED" / "MOHAMED")
- Produce `dedup_report.csv` with: entity IDs, names, countries, similarity score, match signals, recommended action
- Merge confirmed duplicates into canonical entities preserving information from both sources

> Perfect dedup isn't expected. We're evaluating your **approach and reasoning**. Document assumptions and limitations.

---

## Part 4 — Database Layer (10 points)

Design and implement a **SQLite** database.

### Schema Requirements

1. Store FtM `Person` and `Occupancy` entities efficiently. Decide how to handle multi-valued properties (JSON columns, junction tables, denormalization) and **justify your choice**
2. Track which source(s) each person came from and whether they were merged during dedup
3. Support search on person names and aliases (SQLite FTS5 or LIKE-based — document your trade-off)
4. Include a `pipeline_runs` table:
   - `run_id`, `started_at`, `completed_at`, `status`, `cia_records_ingested`, `ofac_records_ingested`, `duplicates_found`, `entities_stored`, `error_message`

### Loader Requirements

- The loader must be **idempotent** — running it twice with the same data should not create duplicates
- Document your idempotency strategy (upsert, delete-and-reload, versioning, etc.)

---

## Part 5 — FastAPI Application (15 points)

Build a **self-contained FastAPI application** that serves as the single entry point for the entire system.

### Startup Behavior

When the app starts via `uvicorn`, it should:

1. **Initialize the database** — create tables if they don't exist
2. **Check if data exists** — if the database is empty (first run), trigger an initial pipeline run
3. **Start the background scheduler** — schedule periodic pipeline refreshes
4. **Begin serving API requests**

Use FastAPI's **lifespan events** (or `@app.on_event("startup")`) to wire this up. The initial pipeline run on first boot can run synchronously (blocking startup briefly) or asynchronously — your choice, but document the trade-off.

### Required Endpoints

#### Person Search & Lookup

**`GET /persons/search`**

The core screening endpoint. A compliance analyst uses this to check if a customer matches any known PEP or sanctioned individual.

Query parameters:
- `q` (required): search query (name or partial name)
- `country`: filter by ISO country code (optional)
- `source`: filter by `cia_world_leaders`, `ofac_sdn`, or `all` (optional, default `all`)
- `limit`: max results (optional, default 20, max 100)
- `offset`: pagination offset (optional, default 0)

Response:
```json
{
  "data": [
    {
      "id": "ofac-12345",
      "schema": "Person",
      "properties": { ... },
      "sources": ["ofac_sdn"],
      "merged_from": null,
      "occupancies": [ ... ]
    }
  ],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

**`GET /persons/{entity_id}`**

Retrieve a single person by entity ID, including all properties, linked occupancies, source information, and dedup metadata.

---

#### Pipeline Observability

**`GET /pipeline/runs`**

List pipeline runs. Support filtering:
- `status`: `success` or `failure` (optional)
- `since`: ISO datetime (optional)
- `limit`: max results (optional, default 10)

**`GET /pipeline/runs/{run_id}`**

Details for a specific run.

**`POST /pipeline/trigger`**

Manually trigger a pipeline run. Returns immediately with:
```json
{
  "run_id": "...",
  "status": "running",
  "message": "Pipeline run started in background"
}
```

The pipeline executes asynchronously in the background and updates the database when complete. Subsequent calls to `GET /pipeline/runs/{run_id}` should reflect progress.

---

#### Statistics & Health

**`GET /stats`**

Summary statistics:
- Total persons, broken down by source
- Top 20 countries by person count
- Total occupancies
- Number of merged/deduplicated entities
- Last successful pipeline run timestamp

**`GET /health`**

Health check:
```json
{
  "status": "ok",
  "database": "connected",
  "scheduler": "running",
  "last_pipeline_run": "2026-05-25T14:30:00Z",
  "next_scheduled_run": "2026-05-26T02:00:00Z"
}
```

---

### API Design Requirements

- Proper HTTP status codes (200, 400, 404, 422, 500)
- Consistent JSON response structure
- Input validation with Pydantic models
- Error handling — bad queries return helpful messages, not tracebacks
- FastAPI auto-generates Swagger docs at `/docs` — make them useful with descriptions, examples, and proper parameter docs

---

## Part 6 — Background Scheduler (5 points)

Integrate a scheduler **within the FastAPI app** (not a separate process).

### Requirements

1. Use **APScheduler** or similar, started during app startup
2. Schedule is configurable via `config.yaml` or environment variables (e.g., `PIPELINE_SCHEDULE=daily` or a cron expression)
3. Each run executes the full pipeline and records results in `pipeline_runs`
4. Failed runs are logged with the error message; the scheduler continues running
5. The scheduler state should be visible in the `/health` endpoint

---

## Part 7 — Short Written Responses (5 points)

Answer in `responses.md`:

1. **Scraping Challenges:** What challenges did you encounter with the CIA site? What would break your scraper, and how would you detect it?

2. **Database Design:** Why did you design the schema this way? How do you handle FtM's multi-valued properties in SQLite? What would you change for PostgreSQL?

3. **Deduplication:** Explain your approach, strengths, failure modes, and what you'd improve with more time.

4. **Production Readiness:** If deployed at a bank, what would you add? Think: authentication, audit logging, monitoring/alerting, data retention, scaling, source downtime handling.

---

## Running the Application

Everything starts with one command:

```bash
# Install dependencies
pip install -r requirements.txt

# Start the application
uvicorn app:app --host 0.0.0.0 --port 8000
```

On first launch:
1. Database tables are created
2. The initial pipeline runs (scraping CIA, downloading OFAC, transforming, deduplicating, loading)
3. The scheduler starts for periodic refreshes
4. The API is live at `http://localhost:8000`
5. Swagger docs are at `http://localhost:8000/docs`

On subsequent launches:
1. Database already exists with data
2. The scheduler starts
3. The API is immediately available
4. Next pipeline run happens on schedule (or via `POST /pipeline/trigger`)

---

## Submission Checklist

- [ ] `app.py` — FastAPI application entry point (or `app/` package with `app/__init__.py`)
- [ ] `ingest/` — ingestion module (scraper + XML parser)
- [ ] `transform/` — schema mapping and normalization
- [ ] `dedup/` — deduplication logic
- [ ] `db/` — database schema, loader, and query helpers
- [ ] `config.yaml` — pipeline and scheduler configuration
- [ ] `compliance.db` — populated SQLite database (from at least one successful run)
- [ ] `dedup_report.csv` — deduplication report
- [ ] `responses.md` — written responses
- [ ] `requirements.txt` — Python dependencies
- [ ] `README.md` — setup instructions, architecture overview, API documentation

---

## Grading Rubric

| Section | Points | Weight |
|---|---|---|
| Part 1 — Data Ingestion (Scraping + XML) | 25 | 25% |
| Part 2 — Schema Mapping & Normalization | 20 | 20% |
| Part 3 — Cross-Source Deduplication | 20 | 20% |
| Part 4 — Database Layer | 10 | 10% |
| Part 5 — FastAPI Application | 15 | 15% |
| Part 6 — Background Scheduler | 5 | 5% |
| Part 7 — Written Responses | 5 | 5% |
| **Total** | **100** | **100%** |

### Bonus (up to +10 points)

- **+3:** Use the `followthemoney` Python library to construct and validate entities
- **+2:** Write tests (unit tests for normalization/dedup, integration tests for API endpoints)
- **+2:** Add a `Dockerfile` where `docker build && docker run` gives a fully working system
- **+2:** Summary analysis: overlap statistics by country, transliteration patterns, data quality metrics
- **+1:** Implement API rate limiting on the search endpoint

---

## Important Notes

- **Both sources are real, public government data.** You are permitted to access them.
- **Scraping etiquette matters.** Be polite with the CIA site: delays, proper User-Agent. If blocked, slow down.
- **Clarity over cleverness.** Simple, documented, working code beats over-engineered code that doesn't.
- **Document your decisions.** Judgment calls should be explained in comments or your write-up.
- **If something is ambiguous, ask.** We'd rather clarify than have you guess.
- **Commit often.** Meaningful commit history shows your process.

Good luck — and welcome to the world of compliance data engineering. 🏦
