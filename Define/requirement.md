# Zeb Agentic Lakebase Accelerator — Backend API Requirements

## Project Objective

Build the **backend API** for the Lakebase Accelerator, delivered as a FastAPI application deployed as a Databricks App. The backend exposes REST endpoints consumed by a **separate frontend team**. It orchestrates agent-native pipelines on top of Lakebase in two modes:

1. **Greenfield mode** (`POST /api/v1/projects/greenfield`): accepts a business prompt → runs the 12-step pipeline via SSE → returns the deployed app URL.
2. **Brownfield mode** (`POST /api/v1/projects/brownfield`): accepts a prompt + file uploads → runs the pipeline via SSE → returns the deployed app URL.

The backend also provides project history listing and detail retrieval for the frontend's chat history panel.

### Scope Boundary

- **In scope**: FastAPI backend, pipeline orchestration, Lakebase operations, LLM integration, Databricks Apps deployment, SSE streaming, project history API.
- **Out of scope**: Frontend/UI development (handled by a separate team), accelerator UI React code.

---

## API Endpoints

The backend exposes 6 endpoints. See `api/openapi.yaml` for the full OpenAPI specification.

| # | Method | Path | Response Type | Purpose |
|---|--------|------|---------------|---------|
| 1 | `POST` | `/api/v1/projects/greenfield` | `text/event-stream` (SSE) | Start greenfield pipeline from a business prompt |
| 2 | `POST` | `/api/v1/projects/brownfield` | `text/event-stream` (SSE) | Start brownfield pipeline from prompt + file uploads |
| 3 | `GET` | `/api/v1/projects` | `application/json` | List all projects (chat history) |
| 4 | `GET` | `/api/v1/projects/{project_id}` | `application/json` | Get project detail (resume view) |
| 5 | `GET` | `/health` | `application/json` | Liveness probe |
| 6 | `GET` | `/ready` | `application/json` | Readiness probe (Lakebase + Model Serving) |

---

## Functional Requirements

### FR-1: Greenfield Pipeline (SSE)

**Endpoint**: `POST /api/v1/projects/greenfield`

The system must:
- Accept a JSON body with `prompt` (required, 1–10000 chars) and optional `project_name` (kebab-case).
- Start the 12-step greenfield pipeline and return a `text/event-stream` SSE response.
- Stream `step_started`, `step_completed`, `step_failed` events for each pipeline step.
- Emit exactly one `pipeline_complete` event as the final event (success or failure).
- On success, the `pipeline_complete` data payload must include: `app_url`, `app_name`, `schema_name`, `catalog`, `tables_created`.
- On failure, the `pipeline_complete` data payload must include: `failed_step`, `error_code`, `completed_steps`.
- Reject empty or oversized prompts with HTTP 422.

**Pipeline Steps (in order)**:
1. Requirement Intake — analyze prompt, extract entities/relationships
2. Data Model Inference — generate PostgreSQL data model
3. Schema Provisioning — create Lakebase schema + tables
4. Seed Data Generation — populate tables with realistic data
5. Frontend Generation — generate React app code
6. Backend Generation — generate FastAPI app code
7. Deployment Config — generate app.yaml, Dockerfile
8. Validation — verify schema, data, and file completeness
9. Workspace Write — write files to Databricks Workspace
10. App Deployment — create and deploy Databricks App
11. Permission Grant — grant SP schema-scoped access
12. Audit — log pipeline execution metadata

### FR-2: Brownfield Pipeline (SSE)

**Endpoint**: `POST /api/v1/projects/brownfield`

The system must:
- Accept a `multipart/form-data` request with `prompt` (required, 1–10000 chars), optional `project_name`, and optional `files` (max 10 files, max 50MB total).
- Supported file types: `.zip`, `.png`, `.jpg`, `.jpeg`, `.pdf`, `.json`, `.yaml`, `.yml`, `.sql`, `.md`, `.txt`.
- Start the brownfield pipeline and return a `text/event-stream` SSE response.
- The first step is `prototype_ingestion` (replaces `requirement_intake`), which reverse-engineers the uploaded artifacts.
- Remaining steps follow the same pattern as greenfield.
- Stream the same SSE event format as greenfield.
- Reject invalid file types or missing prompt with HTTP 422.

**Pipeline Steps (in order)**:
1. Prototype Ingestion — reverse-engineer uploaded prototype
2. Data Model Inference — generate PostgreSQL data model from inferred entities
3. Schema Provisioning — create Lakebase schema + tables
4. Seed Data Generation — extract/generate seed data from prototype
5. Frontend Generation — generate React app code
6. Backend Generation — generate FastAPI app code
7. Deployment Config — generate app.yaml, Dockerfile
8. Validation — verify schema, data, and file completeness
9. Workspace Write — write files to Databricks Workspace
10. App Deployment — create and deploy Databricks App
11. Permission Grant — grant SP schema-scoped access
12. Audit — log pipeline execution metadata

### FR-3: Project Listing (Chat History)

**Endpoint**: `GET /api/v1/projects`

The system must:
- Return a paginated list of all projects ordered by `created_at` descending.
- Each project summary includes: `id`, `project_name`, `mode` (greenfield/brownfield), `status`, `prompt_preview` (first 100 chars), `app_url`, `created_at`.
- Support optional query filters: `mode`, `status`, `limit` (default 50, max 100), `offset` (default 0).
- Return `total` count for pagination.

### FR-4: Project Detail (Resume View)

**Endpoint**: `GET /api/v1/projects/{project_id}`

The system must:
- Return full project details including: original prompt, app_name, app_url, schema_name, catalog, tables_created, pipeline_duration_seconds, total_token_usage.
- Include ordered pipeline step history with per-step: step name, status, duration_ms, token_usage, error_message, error_code.
- Return HTTP 404 if project not found.

### FR-5: Health Check

**Endpoint**: `GET /health`

The system must:
- Return `{"status": "healthy", "version": "1.0.0"}` with HTTP 200 if the application is running.

### FR-6: Readiness Check

**Endpoint**: `GET /ready`

The system must:
- Check Lakebase connectivity and Model Serving endpoint readiness.
- Return HTTP 200 with `{"status": "ready", "checks": {...}}` if all dependencies are available.
- Return HTTP 503 with failing check details if any dependency is unavailable.

---

## SSE Event Format

Both pipeline endpoints (`/greenfield` and `/brownfield`) use the same SSE event format:

```
event: {event_type}
data: {"step": "...", "status": "...", "message": "...", "data": {...}, "timestamp": "..."}
```

**Event types**:
- `step_started` — emitted when a step begins (status: `running`)
- `step_completed` — emitted when a step succeeds (status: `completed`)
- `step_failed` — emitted when a step fails (status: `failed`)
- `pipeline_complete` — emitted exactly once as the final event

**Success payload** (in `pipeline_complete`):
```json
{
  "app_url": "https://adb-xxx.azuredatabricks.net/apps/app-name",
  "app_name": "AppDisplayName",
  "schema_name": "project_app_name_a1b2c3",
  "catalog": "zeb_prod",
  "tables_created": ["table1", "table2", "table3"]
}
```

**Failure payload** (in `pipeline_complete`):
```json
{
  "failed_step": "schema_provisioning",
  "error_code": "SCHEMA_CREATION_FAILED",
  "completed_steps": ["requirement_intake", "data_model_inference"]
}
```

---

## Non-Functional Requirements

### Performance
- Full greenfield pipeline must complete within 5 minutes (300-second timeout).
- SSE events must be flushed immediately (no buffering).
- Project listing must respond within 200ms.
- Project detail must respond within 500ms.

### Security
- All Lakebase operations use parameterized queries (no SQL injection).
- Service principal credentials are injected via Databricks Apps resources (never hardcoded).
- File uploads are validated for type and size before processing.
- No database credentials exposed in API responses.

### Reliability
- Schema creation handles naming collisions with retry (max 3 attempts).
- LLM calls retry up to 2 times on 5xx/timeout with exponential backoff.
- App deployment polls with 10-second intervals, 300-second timeout.
- Partial pipeline failures preserve completed step results.
- Clear error codes and messages for every failure mode.

### Observability
- Every pipeline step is logged with: step name, duration, token usage, status.
- All LLM calls are tracked in `accelerator_meta.prompts` and `accelerator_meta.model_consumption`.
- Errors are logged in `accelerator_meta.error_logs` with trace_id correlation.
- Structured JSON logging with trace_id across all operations.

---

## Technology Stack (Backend Only)

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Framework | **FastAPI** | REST API + SSE streaming |
| Server | **uvicorn** | ASGI server |
| Validation | **Pydantic v2** | Request/response validation, LLM output parsing |
| Database | **psycopg2** | Lakebase (PostgreSQL) connection with connection pooling |
| LLM | **Databricks SDK** | Model Serving endpoint calls |
| Infrastructure | **Databricks REST API** | App creation, deployment, workspace files |
| Templates | **Jinja2** | Generated app code templates |
| Streaming | **sse-starlette** | Server-Sent Events support |
| Config | **pydantic-settings** | Environment variable management |
| HTTP Client | **httpx** | Async HTTP calls to Databricks APIs |

---

## Data Model (Accelerator Internal)

The accelerator maintains its own `accelerator_meta` schema with these tables:
- `projects` — tracks all pipeline executions (greenfield + brownfield)
- `prompts` — stores all LLM prompts and responses
- `model_consumption` — tracks token usage and costs per LLM call
- `error_logs` — records all errors with context and trace_id

See `design/er_diagram.mmd` for the full ER diagram.

---

## Acceptance Criteria

### Greenfield API
- `POST /api/v1/projects/greenfield` with a valid prompt returns SSE stream.
- SSE stream emits `step_started` + `step_completed` for each successful step.
- Final `pipeline_complete` event contains `app_url`, `schema_name`, `catalog`, `tables_created`.
- Invalid prompts return HTTP 422 with error details.

### Brownfield API
- `POST /api/v1/projects/brownfield` with prompt + files returns SSE stream.
- File uploads are validated (type, size, count).
- SSE stream follows same format as greenfield.
- Final `pipeline_complete` event contains deployment result.

### History API
- `GET /api/v1/projects` returns paginated list with mode tags.
- `GET /api/v1/projects/{id}` returns full detail with step history.
- Non-existent project returns HTTP 404.

### Health API
- `GET /health` returns 200 with version.
- `GET /ready` returns 200 when dependencies are up, 503 when down.

---

## Implementation Order

1. Project scaffolding (pyproject.toml, settings, exceptions, logger)
2. Pydantic models (requests, responses, pipeline events, LLM models)
3. Core algorithms (topological sort, schema naming)
4. LLM client (Model Serving integration with retry)
5. Repositories (Lakebase, Databricks Apps, Workspace Files)
6. Services (pipeline steps: intake, inference, provisioning, seed data, generation, deployment, permissions, audit)
7. Pipeline orchestrator (SSE streaming, step sequencing, error handling)
8. Routes (greenfield, brownfield, projects, health)
9. Dependency injection wiring
10. Deployment config (Dockerfile, app.yaml)

---

## Assumptions

- The frontend team consumes this API and handles all UI concerns.
- The client's Databricks workspace has Lakebase, Model Serving, and Databricks Apps enabled.
- The accelerator runs with SP_Accelerator credentials (broad workspace permissions).
- Generated apps use React + FastAPI as the application stack.
- CORS is configured to allow the frontend origin.

---

## Out of Scope (This Backend)

- Frontend/UI development (separate team).
- Reverse ETL from Unity Catalog.
- Unity Catalog function registration.
- Multi-workspace deployment.
- Authentication/authorization of API callers (handled by Databricks Apps platform).
