# Zeb Agentic Lakebase Accelerator — Agent Tools

## Overview

These are the agent tools used by the accelerator to execute the greenfield and brownfield flows. Each tool is a plain Python function in the FastAPI backend — no Unity Catalog function registration. Tools are called by the agent orchestrator in sequence, with the LLM (via Model Serving) providing reasoning at each step.

All tools operate **within the client's Databricks workspace**. The accelerator runs as a deployed bundle with its own service principal (SP_Accelerator).

---

## Tool Categories

### A. Input & Analysis Tools

#### 1. Requirement Intake Tool
**Used in**: Greenfield
**Purpose**: Accepts a natural-language business prompt from the chat interface and extracts structured requirements.

**What it does**:
- Sends the user's prompt to Model Serving for analysis.
- Extracts business entities (with attributes and data types), relationships (with cardinality), workflows, user roles, and required actions.
- Returns a structured `AnalysisResult` (Pydantic model) consumed by downstream tools.
- If the prompt is too vague, generates clarification questions for the user.

**Input**: Business prompt string.
**Output**: `AnalysisResult` — entities, relationships, workflows, project name.

---

#### 2. Prototype Ingestion Tool
**Used in**: Brownfield only
**Purpose**: Accepts an existing prototype and reverse-engineers it to extract application structure.

**What it does**:
- Accepts one or more of: code repository (uploaded or URL), UI screenshots/mockups, API specs/OpenAPI definitions, sample JSON payloads, database schema exports.
- Uses Model Serving (including vision capabilities for screenshots) to infer: business entities and relationships, UI screens/forms/navigation flows, API endpoints and data access patterns, workflows and business rules.
- Identifies production gaps: missing backend design, missing data validation/constraints, missing auth, missing error handling, missing operational database.
- Returns a structured `PrototypeAnalysis` (Pydantic model) with inferred entities, gaps, and migration plan.

**Input**: Prototype artifacts (files, URLs, screenshots).
**Output**: `PrototypeAnalysis` — entities, relationships, screens, gaps, migration recommendations.

---

### B. Data Model & Schema Tools

#### 3. Data Model Inference Tool
**Used in**: Both flows
**Purpose**: Converts analysis results into a concrete PostgreSQL data model for Lakebase.

**What it does**:
- Takes the `AnalysisResult` (greenfield) or `PrototypeAnalysis` (brownfield) and generates a complete relational data model.
- Defines tables, columns with PostgreSQL data types, primary keys, foreign keys, NOT NULL constraints, and indexes.
- Computes table creation order (topological sort) to respect foreign key dependencies.
- Returns a structured `DataModel` (Pydantic model) with DDL-ready definitions.

**Input**: `AnalysisResult` or `PrototypeAnalysis`.
**Output**: `DataModel` — tables, columns, constraints, indexes, creation order.

---

#### 4. Lakebase Schema Provisioning Tool
**Used in**: Both flows
**Purpose**: Creates a dedicated Lakebase schema and tables for the project.

**What it does**:
- Generates a unique schema name derived from the project context (valid PostgreSQL identifier, max 63 chars).
- Connects to Lakebase using SP_Accelerator credentials (injected via Databricks Apps resource).
- Executes `CREATE SCHEMA` DDL.
- Executes `CREATE TABLE` statements in topological order (respecting FK dependencies).
- Creates indexes on foreign key columns and likely filter columns.
- Handles naming collisions by appending a disambiguating suffix and retrying.
- Uses parameterized queries and psycopg2 for all database operations.

**Input**: `DataModel`, project name.
**Output**: Schema name, list of created table names.

---

#### 5. Schema Evolution Tool
**Used in**: Both flows (iterative updates)
**Purpose**: Safely modifies existing Lakebase schema when the data model changes.

**What it does**:
- Compares the current schema state with the desired `DataModel`.
- Generates `ALTER TABLE` statements for adding columns, modifying types, or adding constraints.
- Generates `CREATE TABLE` for new entities.
- Executes migrations within a transaction for atomicity.
- Logs all schema changes for auditability.

**Input**: Schema name, updated `DataModel`.
**Output**: List of applied migrations.

---

### C. Data Tools

#### 6. Seed Data Generation Tool
**Used in**: Both flows
**Purpose**: Populates tables with realistic sample data.

**What it does**:
- **Greenfield**: Calls Model Serving to generate domain-appropriate seed data based on the inferred entities (minimum 5 rows per table).
- **Brownfield**: Extracts sample/mock data from the prototype artifacts, or generates realistic data based on the inferred model if no data is available.
- Validates seed data against all constraints (NOT NULL, data types, foreign key references) before insertion.
- Inserts data in topological order to satisfy referential integrity.
- On constraint violation, regenerates the violating rows and retries once.

**Input**: Schema name, `DataModel`, prototype data (brownfield only).
**Output**: Row counts per table.

---

#### 7. Query Tool
**Used in**: Both flows (runtime)
**Purpose**: Reads operational records from Lakebase for agent reasoning.

**What it does**:
- Executes parameterized SELECT queries against the project's schema.
- Returns results as structured data (list of dicts).
- Supports the agent's operational memory — lets it answer questions using current app state.
- Scoped to the project's schema only.

**Input**: SQL query string, parameters.
**Output**: Query results as list of dicts.

---

#### 8. Write/Update Tool
**Used in**: Both flows (runtime)
**Purpose**: Creates or modifies operational records in Lakebase.

**What it does**:
- Executes parameterized INSERT, UPDATE, or DELETE statements against the project's schema.
- Supports transactional operations (commit/rollback).
- Used by the agent to update workflow status, save user actions, record audit events.
- Scoped to the project's schema only.

**Input**: SQL statement, parameters.
**Output**: Affected row count.

---

### D. Application Generation Tools

#### 9. React Frontend Generation Tool
**Used in**: Both flows
**Purpose**: Generates a React (Vite + TypeScript) frontend tailored to the project's entities.

**What it does**:
- Calls Model Serving to generate React component code based on the `DataModel`.
- Generates per-entity pages: list view, detail view, create form, edit form.
- Generates navigation/routing using React Router.
- Generates API client layer (axios) for calling the FastAPI backend.
- Generates `package.json`, `vite.config.ts`, `tsconfig.json`.
- Optionally applies Tailwind CSS for styling.
- Uses Jinja2 templates as base scaffolding, with LLM filling in entity-specific logic.

**Input**: `DataModel`, project name, UX preferences (optional).
**Output**: Dict of filename → content (React source files).

---

#### 10. FastAPI Backend Generation Tool
**Used in**: Both flows
**Purpose**: Generates a FastAPI backend with CRUD endpoints for each entity.

**What it does**:
- Generates route modules with REST endpoints (GET list, GET detail, POST create, PUT update, DELETE) per entity.
- Generates Pydantic request/response models for each entity.
- Generates database connection module using psycopg2 with connection pooling.
- Generates `main.py` with FastAPI app, CORS config, and route registration.
- Generates `requirements.txt` with pinned dependencies.
- Generates health check and readiness endpoints.
- All database queries use parameterized statements (no raw SQL interpolation).

**Input**: `DataModel`, schema name, project name.
**Output**: Dict of filename → content (FastAPI source files).

---

#### 11. Deployment Config Generation Tool
**Used in**: Both flows
**Purpose**: Generates all configuration files needed for Databricks Apps deployment.

**What it does**:
- Generates `app.yaml` with: Streamlit/FastAPI command, Lakebase resource attachment, Model Serving resource (if needed), environment variables (LAKEBASE_SCHEMA, etc.).
- Generates `Dockerfile` for building the React + FastAPI app.
- Generates `.env.sample` with required environment variable documentation.
- Ensures the generated config matches Databricks Apps requirements.

**Input**: Project name, schema name, resource requirements.
**Output**: Dict of filename → content (app.yaml, Dockerfile, .env.sample).

---

### E. Deployment & Infrastructure Tools

#### 12. Workspace File Writer Tool
**Used in**: Both flows
**Purpose**: Writes generated application files to the client's Databricks workspace.

**What it does**:
- Writes each generated file to `/Workspace/Apps/{app_name}/` via the Workspace Files API.
- Creates directory structure as needed.
- Uses SP_Accelerator credentials for workspace access.
- Retries on transient errors with exponential backoff.

**Input**: App name, dict of filename → content.
**Output**: Workspace path where files were written.

---

#### 13. App Deployment Tool
**Used in**: Both flows
**Purpose**: Creates and deploys a Databricks App via the REST API.

**What it does**:
- Creates a new Databricks App via `POST /api/2.0/apps` with Lakebase attached as a resource.
- Creates a deployment via `POST /api/2.0/apps/{name}/deployments` pointing to the workspace source path.
- Polls deployment status until READY (timeout: 5 minutes).
- Returns the live app URL.
- Retries on transient 5xx errors with exponential backoff.
- Reports descriptive errors on 4xx failures or deployment timeout.

**Input**: App name, workspace path, Lakebase resource config.
**Output**: Deployed app URL.

---

#### 14. Service Principal Permission Tool
**Used in**: Both flows
**Purpose**: Grants the generated app's service principal access to only its own Lakebase schema.

**What it does**:
- Discovers the auto-created service principal for the newly deployed app via `GET /api/2.0/apps/{name}`.
- Executes GRANT statements on Lakebase: `GRANT USAGE ON SCHEMA`, `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA`.
- Grants permissions ONLY on the target schema — no cross-schema access.
- Reports failure with guidance if permission granting fails.

**Input**: App name, schema name.
**Output**: Confirmation of grants applied.

---

### F. LLM & Reasoning Tools

#### 15. Model Serving Call Tool
**Used in**: Both flows
**Purpose**: Calls the Databricks Model Serving endpoint for all LLM reasoning.

**What it does**:
- Sends structured prompts (system + user) to the Model Serving endpoint.
- Requests JSON-formatted responses with optional JSON Schema for structured output.
- Parses and validates responses using Pydantic models.
- Retries up to 2 times on transient errors (5xx, timeout) or malformed JSON with exponential backoff.
- Checks endpoint readiness before first call; halts if not READY.
- Authenticates using SP_Accelerator credentials via Databricks SDK.

**Input**: System prompt, user prompt, optional response schema.
**Output**: Parsed JSON response (dict).

---

### G. Validation & Observability Tools

#### 16. Validation Tool
**Used in**: Both flows (pre-deployment)
**Purpose**: Validates the generated application before deployment.

**What it does**:
- Validates schema exists and tables are created correctly in Lakebase.
- Validates seed data was inserted (row counts > 0 per table).
- Validates generated app files are complete (app.py/main.py, app.yaml, requirements.txt/package.json).
- Validates Lakebase connectivity from the generated app's connection config.
- Validates Model Serving endpoint is reachable (if the generated app uses it).
- Returns a validation report with pass/fail per check.

**Input**: Schema name, app file dict, resource config.
**Output**: Validation report (list of check results).

---

#### 17. Monitoring & Audit Tool
**Used in**: Both flows (post-deployment)
**Purpose**: Tracks agent actions, deployment status, and system health.

**What it does**:
- Logs each pipeline step with: step name, duration, token usage (for LLM calls), success/failure status.
- Records deployment metadata: app name, schema name, app URL, timestamp, SP grants.
- Stores audit records in the accelerator's own Lakebase schema (not the generated app's schema).
- Provides deployment history and status queries for the accelerator UI.

**Input**: Step name, metadata, status.
**Output**: Audit record ID.

---

## Tool Execution Flow

### Greenfield Flow
```
User Prompt
  → [1] Requirement Intake Tool
  → [3] Data Model Inference Tool
  → [4] Lakebase Schema Provisioning Tool
  → [6] Seed Data Generation Tool
  → [9] React Frontend Generation Tool
  → [10] FastAPI Backend Generation Tool
  → [11] Deployment Config Generation Tool
  → [16] Validation Tool
  → [12] Workspace File Writer Tool
  → [13] App Deployment Tool
  → [14] Service Principal Permission Tool
  → [17] Monitoring & Audit Tool
  → Return App URL
```

### Brownfield Flow
```
Prototype Upload
  → [2] Prototype Ingestion Tool
  → [3] Data Model Inference Tool
  → [4] Lakebase Schema Provisioning Tool
  → [6] Seed Data Generation Tool (with prototype data)
  → [9] React Frontend Generation Tool
  → [10] FastAPI Backend Generation Tool
  → [11] Deployment Config Generation Tool
  → [16] Validation Tool
  → [12] Workspace File Writer Tool
  → [13] App Deployment Tool
  → [14] Service Principal Permission Tool
  → [17] Monitoring & Audit Tool
  → Return App URL
```

---

## Tools Summary Table

| # | Tool | Greenfield | Brownfield | Databricks Service Used |
|---|------|-----------|------------|------------------------|
| 1 | Requirement Intake | ✅ | ❌ | Model Serving |
| 2 | Prototype Ingestion | ❌ | ✅ | Model Serving |
| 3 | Data Model Inference | ✅ | ✅ | Model Serving |
| 4 | Lakebase Schema Provisioning | ✅ | ✅ | Lakebase |
| 5 | Schema Evolution | ✅ (iterative) | ✅ (iterative) | Lakebase |
| 6 | Seed Data Generation | ✅ | ✅ | Model Serving + Lakebase |
| 7 | Query | ✅ (runtime) | ✅ (runtime) | Lakebase |
| 8 | Write/Update | ✅ (runtime) | ✅ (runtime) | Lakebase |
| 9 | React Frontend Generation | ✅ | ✅ | Model Serving |
| 10 | FastAPI Backend Generation | ✅ | ✅ | Model Serving |
| 11 | Deployment Config Generation | ✅ | ✅ | None (local generation) |
| 12 | Workspace File Writer | ✅ | ✅ | Workspace Files API |
| 13 | App Deployment | ✅ | ✅ | Databricks Apps API |
| 14 | Service Principal Permission | ✅ | ✅ | Lakebase + Apps API |
| 15 | Model Serving Call | ✅ | ✅ | Model Serving |
| 16 | Validation | ✅ | ✅ | Lakebase + Model Serving |
| 17 | Monitoring & Audit | ✅ | ✅ | Lakebase |

---

## Removed Tools (from previous version)

| Old Tool | Reason Removed |
|----------|---------------|
| Reverse ETL Mapping Tool | Reverse ETL is out of scope for initial delivery. No Gold/Silver sync needed — seed data is generated or extracted from prototypes. |
| Unity Catalog Function Tools | Agent tools are plain Python functions in the FastAPI backend. No UC function registration needed. |

---

## Implementation Notes

- All tools are plain Python functions following the layered architecture: Routes → Services → Repository → Data Store.
- Tools use Pydantic models for input/output validation.
- All Lakebase operations use parameterized queries via psycopg2 (no raw SQL interpolation).
- All Model Serving calls go through the centralized LLM Client with retry logic.
- Error handling follows the `PipelineStepError` pattern — each tool raises with its step name for clear error reporting.
- Tools are organized under `src/lakebase_accelerator/services/` following the project's architecture standards.
