# Deployment Fixes — Applied to Agent Pipeline

## Status: ✅ ALL FIXES APPLIED

The generated app `simple-notes-app-a5660f` is live at:
`https://simple-notes-app-a5660f-2356669523491617.aws.databricksapps.com`

All fixes have been applied to the agent pipeline nodes. The pipeline now includes
a **self-healing deployment retry loop** that automatically feeds error logs back
to the backend_dev node for correction.

---

## Issue 1: Broken Import Path (`ImportError: No module named 'routes'`)

### Symptom
`app.py` does `import routes` but routes live at `src/routes/`. App crashes on startup with `ImportError`.

### Root Cause
`backend_dev_node` prompt tells the LLM to put routes in `src/routes/`, but doesn't enforce the correct import in `app.py`.

### Fix for Agent
In `BACKEND_FILE_PROMPT`, add:
```
- app.py must import routes using: `from src.routes import router`
- Do NOT use `import routes` — the routes package is inside src/
- Register with: `app.include_router(router)`
- src/routes/__init__.py must ONLY re-export: `from .notes import router`
```

---

## Issue 2: Workspace Import Creates Notebooks Instead of Plain Files

### Symptom
`Error loading ASGI app. Could not import module "app"` — uvicorn can't find `app.py` because it was stored as a Databricks Notebook, not a plain Python file.

### Root Cause
The `deployment_node._write_file()` uses `/api/2.0/workspace/import` with `format: "SOURCE"` and `language: "PYTHON"`. This creates a **Notebook**, not a plain file. Uvicorn needs actual `.py` files on disk.

### Fix for Agent
In `src/lakebase_accelerator/agent/nodes/deployment.py`, the `_write_file()` function must:

```python
def _write_file(host: str, token: str, path: str, content: str) -> bool:
    """Write a plain file to workspace. NEVER use format=SOURCE with language=PYTHON."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    # Create parent directory
    parent = "/".join(path.rsplit("/", 1)[:-1])
    httpx.post(f"{host}/api/2.0/workspace/mkdirs", json={"path": parent}, headers=headers, timeout=15.0)

    # Encode content
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    # CRITICAL: Use format="AUTO" WITHOUT language parameter
    # format="AUTO" without language → creates a plain FILE
    # format="SOURCE" with language="PYTHON" → creates a NOTEBOOK (WRONG!)
    resp = httpx.post(
        f"{host}/api/2.0/workspace/import",
        json={"path": path, "content": encoded, "format": "AUTO", "overwrite": True},
        headers=headers,
        timeout=15.0,
    )
    
    if resp.status_code == 200:
        return True

    # If overwrite fails (existing notebook at path), delete first then retry
    httpx.post(
        f"{host}/api/2.0/workspace/delete",
        json={"path": path, "recursive": False},
        headers=headers,
        timeout=15.0,
    )
    resp = httpx.post(
        f"{host}/api/2.0/workspace/import",
        json={"path": path, "content": encoded, "format": "AUTO", "overwrite": True},
        headers=headers,
        timeout=15.0,
    )
    return resp.status_code == 200
```

**Key rule**: NEVER pass `"language": "PYTHON"` to the workspace import API. This converts files to notebooks.

Also: Before writing files, **delete the entire workspace directory** first to remove any stale notebooks:
```python
# Clean workspace path before writing
httpx.post(
    f"{host}/api/2.0/workspace/delete",
    json={"path": workspace_path, "recursive": True},
    headers=headers,
    timeout=15.0,
)
```

---

## Issue 3: Missing Lakebase OAuth Token Generation in Generated App

### Symptom
App can't connect to database — no `POSTGRES_PASSWORD` env var set, and Lakebase requires OAuth tokens.

### Root Cause
Generated `src/database.py` uses `os.getenv("POSTGRES_PASSWORD")` but Lakebase uses OAuth token rotation (60-min lifetime). No static password exists.

### Fix for Agent
The `BACKEND_FILE_PROMPT` for `src/database.py` must include OAuth token generation:

```
- src/database.py MUST support OAuth token rotation for Lakebase:
  1. If POSTGRES_PASSWORD is set, use it directly (static auth)
  2. If DATABRICKS_HOST + DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET + LAKEBASE_ENDPOINT_NAME are set:
     a. POST to {DATABRICKS_HOST}/oidc/v1/token with client_credentials grant → get workspace token
     b. POST to {DATABRICKS_HOST}/api/2.0/postgres/credentials with workspace token → get DB password
     c. Use the returned token as the psycopg2 password
  3. Tokens expire after 60 minutes — detect stale connections and refresh the pool
- ALWAYS use sslmode="require" for Lakebase connections
- Use search_path option: options=f"-c search_path={LAKEBASE_SCHEMA}"
- requirements.txt MUST include httpx for OAuth HTTP calls
```

---

## Issue 4: Missing Environment Variables in app.yaml

### Symptom
App starts but can't connect to database — missing POSTGRES_HOST, DATABRICKS_HOST, etc.

### Root Cause
`_generate_app_yaml()` only sets `LAKEBASE_SCHEMA`. The app needs all connection + OAuth env vars.

### Fix for Agent
Both `backend_dev_node._generate_app_yaml()` and `integration_node._generate_app_yaml()` must inject all env vars from settings:

```python
def _generate_app_yaml(schema_name: str, app_name: str) -> str:
    from lakebase_accelerator.settings import get_settings
    s = get_settings()
    
    return f"""command:
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

env:
  - name: "LAKEBASE_SCHEMA"
    value: "{schema_name}"
  - name: "POSTGRES_HOST"
    value: "{s.postgres_host}"
  - name: "POSTGRES_PORT"
    value: "{s.postgres_port}"
  - name: "POSTGRES_USER"
    value: "{s.postgres_user}"
  - name: "POSTGRES_DB"
    value: "{s.postgres_db}"
  - name: "DATABRICKS_HOST"
    value: "{s.databricks_host}"
  - name: "DATABRICKS_CLIENT_ID"
    value: "{s.databricks_client_id}"
  - name: "DATABRICKS_CLIENT_SECRET"
    value: "{s.databricks_client_secret}"
  - name: "LAKEBASE_ENDPOINT_NAME"
    value: "{s.lakebase_endpoint_name}"
"""
```

---

## Issue 5: NULL Column Values Crash Response Serialization

### Symptom
`ResponseValidationError: 'Input should be a valid string', 'input': None` on `GET /api/notes`.
POST works (201 Created) but GET fails (500) because seed data has NULL content.

### Root Cause
- The `notes` table schema allows `content TEXT` (nullable)
- The seed data node may insert rows with `content = NULL`
- The Pydantic `NoteResponse` model declares `content: str` (non-nullable)
- FastAPI's response validation rejects `None` for a `str` field

### Fix for Agent
In `BACKEND_FILE_PROMPT`, add:

```
- ALL Pydantic response model fields that correspond to nullable DB columns MUST use Optional[str]:
  - If the column is defined as TEXT (without NOT NULL), use: content: Optional[str] = None
  - If the column is defined as TEXT NOT NULL, use: content: str
- In SELECT queries, use COALESCE for nullable text columns:
  COALESCE(content, '') as content
- This prevents FastAPI ResponseValidationError when NULL values come from the database
```

Also in the **seed_data_node** prompt, add:
```
- ALL text/varchar columns that are NOT NULL in the schema MUST have non-null values in seed data
- If a column allows NULL, you may include NULL values, but the API must handle them
```

---

## Summary of All Agent Node Changes

### `src/lakebase_accelerator/agent/nodes/deployment.py`

| Change | Description |
|--------|-------------|
| `_write_file()` | Use `format="AUTO"` without `language` param (NEVER `format="SOURCE"` + `language="PYTHON"`) |
| `_write_file()` | Delete existing file before retry (handles notebook→file conversion) |
| `deployment_node()` | Delete workspace directory recursively before writing new files |

### `src/lakebase_accelerator/agent/nodes/backend_dev.py`

| Change | Description |
|--------|-------------|
| `BACKEND_PLAN_PROMPT` | Clarify `src/routes/__init__.py` only re-exports router |
| `BACKEND_FILE_PROMPT` | Fix import: `from src.routes import router` (not `import routes`) |
| `BACKEND_FILE_PROMPT` | Add OAuth token generation requirement for `src/database.py` |
| `BACKEND_FILE_PROMPT` | Add `httpx` to requirements.txt |
| `BACKEND_FILE_PROMPT` | Add `sslmode="require"` requirement |
| `BACKEND_FILE_PROMPT` | Add nullable column handling: `Optional[str]` + `COALESCE` |
| `_generate_app_yaml()` | Include ALL Lakebase + OAuth env vars from settings |

### `src/lakebase_accelerator/agent/nodes/integration.py`

| Change | Description |
|--------|-------------|
| `_generate_app_yaml()` | Include ALL Lakebase + OAuth env vars from settings |
| Validation | Check `app.py` imports from `src.routes` (not bare `import routes`) |
| Validation | Check `requirements.txt` includes `httpx` |

### `src/lakebase_accelerator/agent/nodes/seed_data.py`

| Change | Description |
|--------|-------------|
| `SEED_DATA_PROMPT` | Ensure NOT NULL columns always get non-null values |

---

## Deployment Checklist (for integration_node validation)

Before deploying, validate:
1. ✅ `app.py` imports from `src.routes` (not bare `import routes`)
2. ✅ `app.yaml` has ALL env vars: LAKEBASE_SCHEMA, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_DB, DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET, LAKEBASE_ENDPOINT_NAME
3. ✅ `requirements.txt` includes `httpx` for OAuth
4. ✅ `src/database.py` has OAuth token generation + stale connection refresh
5. ✅ All connections use `sslmode="require"`
6. ✅ `src/routes/__init__.py` only re-exports (no inline implementations)
7. ✅ Pydantic response models use `Optional[str]` for nullable DB columns
8. ✅ SELECT queries use `COALESCE` for nullable text columns
9. ✅ File upload uses `format="AUTO"` without `language` (plain files, NOT notebooks)
10. ✅ Workspace path is cleaned (recursive delete) before writing new files
