"""Backend Dev Agent — Node 5.

Generates FastAPI backend with FLAT structure matching Databricks Apps:
  /app.py              ← entry point
  /requirements.txt    ← dependencies
  /app.yaml            ← Databricks App manifest
  /src/routes/         ← route modules
  /src/models/         ← Pydantic models (if needed)
  /src/database.py     ← DB connection with OAuth token rotation
  /src/__init__.py

Validates each file with Python AST parsing. Retries on errors.

Self-Healing: If deployment_fix_context is present in state, this node
regenerates only the broken files using the error context.
"""

import ast
import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger

BACKEND_PLAN_PROMPT = """You are a FastAPI backend architect. Plan files for a CRUD API.

The app deploys on Databricks Apps with this FLAT structure (no backend/ prefix):
- app.py (root) — FastAPI entry, CORS, route registration, serves static/ at root
- requirements.txt (root) — pinned deps (MUST include httpx for OAuth)
- src/__init__.py — MUST be empty or just a docstring. Do NOT import anything here.
- src/database.py — psycopg2 pool with OAuth token rotation for Lakebase
- src/models/__init__.py — Pydantic models per entity (if needed)
- src/routes/__init__.py — ONLY re-exports router: `from .{entity} import router`
- src/routes/{entity}.py — CRUD endpoints per entity

CRITICAL RULES:
- src/__init__.py MUST be empty or contain only a docstring. NEVER import from database or routes here.
- src/routes/__init__.py must ONLY contain: `from .{entity} import router` and `__all__ = ["router"]`
- Do NOT put route implementations in __init__.py
- app.py imports routes as: `from src.routes import router`
- app.py imports database as: `from src.database import init_db, close_db` (or similar)

Return ONLY a JSON array of file paths:
["app.py", "requirements.txt", "src/__init__.py", "src/database.py", "src/routes/__init__.py", "src/routes/notes.py"]
"""

BACKEND_FILE_PROMPT = """Generate COMPLETE Python code for: {file_path}

Project schema: {schema_name}
Data model:
{data_model_summary}

RULES:
- Use psycopg2.sql module for identifiers (sql.Identifier), NEVER f-strings in SQL
- Use %s for parameterized values
- Database env vars: POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, LAKEBASE_SCHEMA
- OAuth env vars: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET, LAKEBASE_ENDPOINT_NAME

IMPORT RULES (CRITICAL):
- app.py must import routes using: `from src.routes import router`
- app.py must import database using: `from src.database import init_db, close_db` (match actual exports)
- Do NOT use `import routes` — the routes package is inside src/
- Register with: `app.include_router(router)`
- src/__init__.py MUST be empty or just a docstring. NEVER import anything in src/__init__.py.
- src/routes/__init__.py must ONLY re-export: `from .{{entity}} import router` + `__all__ = ["router"]`

DATABASE RULES (CRITICAL):
- src/database.py MUST support OAuth token rotation for Lakebase:
  1. If POSTGRES_PASSWORD is set, use it directly (static auth)
  2. If DATABRICKS_HOST + DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET + LAKEBASE_ENDPOINT_NAME are set:
     a. POST to {{DATABRICKS_HOST}}/oidc/v1/token with client_credentials grant → get workspace token
     b. POST to {{DATABRICKS_HOST}}/api/2.0/postgres/credentials with workspace token → get DB password
     c. Use the returned token as the psycopg2 password
  3. Tokens expire after 60 minutes — detect stale connections and refresh the pool
- ALWAYS use sslmode="require" for Lakebase connections
- Use search_path option: options=f"-c search_path={{LAKEBASE_SCHEMA}}"
- requirements.txt MUST include httpx for OAuth HTTP calls

PYDANTIC MODEL RULES (CRITICAL):
- ALL Pydantic response model fields for nullable DB columns MUST use Optional[str] = None
- If column is TEXT (without NOT NULL): use `content: Optional[str] = None`
- If column is TEXT NOT NULL: use `content: str`
- In SELECT queries, use COALESCE for nullable text columns: `COALESCE(content, '') as content`
- This prevents FastAPI ResponseValidationError when NULL values come from the database

STATIC FILES:
- app.py command: uvicorn app:app (NOT main:app)
- Include /health endpoint in app.py
- Mount StaticFiles(directory="static", html=True) at "/" LAST (after all API routes)

OUTPUT:
- File MUST be syntactically valid Python — no truncation
- Return ONLY Python code, no markdown
- ONLY import from these packages: fastapi, pydantic, psycopg2, httpx, uuid, datetime, typing, os, json
- Do NOT import bcrypt, passlib, jose, jwt, or any other package not in requirements.txt
"""

BACKEND_FIX_PROMPT = """The deployed app CRASHED. Fix the code based on these error logs.

{fix_context}

Generate the COMPLETE fixed Python code for: {file_path}
Apply the fix described above. Return ONLY valid Python code, no markdown.
"""


def backend_dev_node(state: PipelineState) -> dict:
    """Generate backend files with validation. Supports self-healing on deployment failure."""
    logger.info("Node: backend_dev — generating", extra={"step": "backend_dev"})

    # Check if this is a self-healing retry
    fix_context = state.get("deployment_fix_context", "")
    if fix_context:
        logger.info("Self-healing mode: fixing backend based on deployment error", extra={"step": "backend_dev"})
        return _fix_backend(state, fix_context)

    # Normal generation flow
    return _generate_backend(state)


def _generate_backend(state: PipelineState) -> dict:
    """Normal backend generation flow.

    Uses templates for boilerplate (app.py, database.py, __init__.py, requirements.txt, app.yaml)
    and LLM only for entity-specific files (routes, models).
    """
    llm = get_llm(max_tokens=2048)
    data_model = state["data_model"]
    schema_name = state["schema_name"]
    app_name = state.get("app_name", "generated-app")
    data_model_summary = _summarize_data_model(data_model)

    # ─── Template-based files (deterministic, no LLM) ────────────────
    backend_files: dict[str, str] = {}

    # app.py — from template
    tables = data_model.get("tables", [])
    first_entity = tables[0]["name"] if tables else "items"
    backend_files["app.py"] = _render_template("app.py.j2", {
        "app_title": state.get("project_name", "Generated App").replace("-", " ").title(),
        "entity_plural": first_entity,
    })

    # database.py — from template (NEVER LLM-generated)
    backend_files["src/database.py"] = _render_template("database.py.j2", {})

    # src/__init__.py — always empty
    backend_files["src/__init__.py"] = '"""Source package."""\n'

    # requirements.txt — fixed
    backend_files["requirements.txt"] = (
        "fastapi==0.115.6\n"
        "uvicorn==0.34.0\n"
        "psycopg2-binary==2.9.10\n"
        "pydantic[email]==2.10.4\n"
        "httpx==0.28.1\n"
    )

    # app.yaml — from settings
    backend_files["app.yaml"] = _generate_app_yaml(schema_name, app_name)

    # ─── LLM-generated files (entity-specific) ───────────────────────

    # Plan which route files to generate
    route_files = [f"src/routes/{t['name']}.py" for t in tables]

    # Generate routes/__init__.py (re-export)
    if tables:
        entity_imports = "\n".join(f"from .{t['name']} import router as {t['name']}_router" for t in tables)
        if len(tables) == 1:
            backend_files["src/routes/__init__.py"] = f'from .{tables[0]["name"]} import router\n\n__all__ = ["router"]\n'
        else:
            # Multiple entities — merge routers
            from_imports = "\n".join(f"from .{t['name']} import router as {t['name']}_router" for t in tables)
            include_lines = "\n".join(f"router.include_router({t['name']}_router)" for t in tables)
            backend_files["src/routes/__init__.py"] = (
                "from fastapi import APIRouter\n\n"
                f"{from_imports}\n\n"
                "router = APIRouter()\n"
                f"{include_lines}\n\n"
                '__all__ = ["router"]\n'
            )
    else:
        backend_files["src/routes/__init__.py"] = (
            "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n__all__ = [\"router\"]\n"
        )

    # Generate each route file using TEMPLATE (not LLM — prevents wrong imports)
    for table in tables:
        file_path = f"src/routes/{table['name']}.py"
        logger.info(f"Generating: {file_path}", extra={"step": "backend_dev"})
        content = _generate_route_from_template(table)
        backend_files[file_path] = content

    logger.info(f"Backend complete: {len(backend_files)} files", extra={"step": "backend_dev"})

    return {
        "backend_files": backend_files,
        "deployment_fix_context": "",
        "deployment_error_logs": "",
        "current_step": "backend_dev",
        "completed_steps": state.get("completed_steps", []) + ["backend_dev"],
    }


def _fix_backend(state: PipelineState, fix_context: str) -> dict:
    """Self-healing: regenerate broken files based on deployment/integration error logs."""
    llm = get_llm(max_tokens=8192)
    backend_files = dict(state.get("backend_files", {}))
    schema_name = state["schema_name"]
    data_model_summary = _summarize_data_model(state["data_model"])

    # Handle ModuleNotFoundError — add missing module to requirements.txt
    if "ModuleNotFoundError: No module named" in fix_context:
        import re
        match = re.search(r"No module named '(\w+)'", fix_context)
        if match:
            missing_module = match.group(1)
            logger.info(f"Self-healing: adding missing module '{missing_module}' to requirements.txt", extra={"step": "backend_dev"})
            current_reqs = backend_files.get("requirements.txt", "")
            if missing_module not in current_reqs:
                backend_files["requirements.txt"] = current_reqs.rstrip() + f"\n{missing_module}\n"

    # Determine which files need fixing based on error analysis
    files_to_fix = _identify_broken_files(fix_context)

    for file_path in files_to_fix:
        # Skip template-based files — re-apply templates instead of LLM fixing
        if file_path == "src/database.py":
            backend_files["src/database.py"] = _render_template("database.py.j2", {})
            continue
        elif file_path == "app.py":
            tables = state["data_model"].get("tables", [])
            first_entity = tables[0]["name"] if tables else "items"
            backend_files["app.py"] = _render_template("app.py.j2", {
                "app_title": state.get("project_name", "Generated App").replace("-", " ").title(),
                "entity_plural": first_entity,
            })
            continue
        elif file_path == "src/__init__.py":
            backend_files["src/__init__.py"] = '"""Source package."""\n'
            continue

        logger.info(f"Self-healing: fixing {file_path}", extra={"step": "backend_dev"})

        prompt = BACKEND_FIX_PROMPT.format(fix_context=fix_context, file_path=file_path)
        prompt += f"\n\nSchema: {schema_name}\nData model:\n{data_model_summary}"
        prompt += "\n\nCRITICAL: Do NOT import modules not in requirements.txt. Only use: fastapi, pydantic, psycopg2, httpx, uuid, datetime, typing, os."

        response = llm.invoke(
            [
                SystemMessage(content="Fix the error. Return ONLY complete, valid Python. No markdown. Do not use external libraries not in requirements.txt."),
                HumanMessage(content=prompt),
            ]
        )
        content = _strip_markdown(response.content)

        # Validate syntax
        if file_path.endswith(".py"):
            error = _check_python_syntax(content)
            if error:
                logger.warning(f"Fix attempt has syntax error in {file_path}: {error}")
                response = llm.invoke(
                    [
                        SystemMessage(content="Fix the syntax error. Return ONLY valid Python."),
                        HumanMessage(content=f"{prompt}\n\nSyntax error in previous attempt: {error}"),
                    ]
                )
                content = _strip_markdown(response.content)

        backend_files[file_path] = content

    # Ensure httpx is in requirements
    if "httpx" not in backend_files.get("requirements.txt", ""):
        backend_files["requirements.txt"] = backend_files.get("requirements.txt", "").rstrip() + "\nhttpx==0.28.1\n"

    # Regenerate app.yaml with full env vars
    backend_files["app.yaml"] = _generate_app_yaml(schema_name, state.get("app_name", "generated-app"))

    # CRITICAL: Force src/__init__.py to be empty
    backend_files["src/__init__.py"] = '"""Source package."""\n'

    logger.info(f"Self-healing complete: fixed {len(files_to_fix)} files", extra={"step": "backend_dev"})

    return {
        "backend_files": backend_files,
        "deployment_fix_context": "",  # Clear after fix
        "deployment_error_logs": "",
        "current_step": "backend_dev",
        "completed_steps": state.get("completed_steps", []) + ["backend_dev_fix"],
    }


def _identify_broken_files(fix_context: str) -> list[str]:
    """Analyze error context to determine which files need fixing."""
    files_to_fix = []

    if "Could not import module" in fix_context or "ImportError" in fix_context:
        files_to_fix.append("app.py")
        files_to_fix.append("src/__init__.py")
        files_to_fix.append("src/routes/__init__.py")
    if "cannot import name" in fix_context:
        # Specific import mismatch — fix the file doing the import AND the target
        files_to_fix.append("src/__init__.py")
        if "database" in fix_context:
            files_to_fix.append("src/database.py")
        if "routes" in fix_context:
            files_to_fix.append("src/routes/__init__.py")
    if "ResponseValidationError" in fix_context or "'input': None" in fix_context:
        # Find route files that need Optional[] fix
        files_to_fix.append("src/routes/__init__.py")
        # Also fix any entity route files mentioned
        if "notes" in fix_context.lower():
            files_to_fix.append("src/routes/notes.py")
        if "bookmark" in fix_context.lower():
            files_to_fix.append("src/routes/bookmarks.py")
        if "task" in fix_context.lower() or "todo" in fix_context.lower():
            files_to_fix.append("src/routes/tasks.py")
    if "connection" in fix_context.lower() or "password" in fix_context.lower() or "auth" in fix_context.lower():
        files_to_fix.append("src/database.py")
    if "ModuleNotFoundError" in fix_context:
        files_to_fix.append("requirements.txt")

    # If we can't identify specific files, fix the most common culprits
    if not files_to_fix:
        files_to_fix = ["app.py", "src/__init__.py", "src/database.py", "src/routes/__init__.py"]

    return list(set(files_to_fix))


def _generate_and_validate(file_path: str, schema_name: str, data_model_summary: str, max_tokens: int) -> str:
    """Generate a file, validate syntax, retry if invalid."""
    llm = get_llm(max_tokens=max_tokens)
    prompt = BACKEND_FILE_PROMPT.format(
        file_path=file_path, schema_name=schema_name, data_model_summary=data_model_summary
    )

    for attempt in range(2):
        response = llm.invoke(
            [
                SystemMessage(content="Generate COMPLETE, syntactically valid Python. No markdown. No truncation."),
                HumanMessage(content=prompt),
            ]
        )
        content = _strip_markdown(response.content)

        # Validate Python syntax
        if file_path.endswith(".py"):
            error = _check_python_syntax(content)
            if error:
                logger.warning(f"Syntax error in {file_path} (attempt {attempt + 1}): {error}")
                prompt += f"\n\nPrevious attempt had syntax error: {error}\nFix it and return complete valid Python."
                continue

        return content

    # Return last attempt even if invalid (better than nothing)
    return content


def _check_python_syntax(code: str) -> str | None:
    """Check Python syntax using AST. Returns error message or None if valid."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"Line {e.lineno}: {e.msg}"


def _generate_app_yaml(schema_name: str, app_name: str) -> str:
    """Generate app.yaml with full Lakebase + OAuth environment variables."""
    settings = get_settings()
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
    value: "{settings.postgres_host}"
  - name: "POSTGRES_PORT"
    value: "{settings.postgres_port}"
  - name: "POSTGRES_USER"
    value: "{settings.postgres_user}"
  - name: "POSTGRES_DB"
    value: "{settings.postgres_db}"
  - name: "DATABRICKS_HOST"
    value: "{settings.databricks_host}"
  - name: "DATABRICKS_CLIENT_ID"
    value: "{settings.databricks_client_id}"
  - name: "DATABRICKS_CLIENT_SECRET"
    value: "{settings.databricks_client_secret}"
  - name: "LAKEBASE_ENDPOINT_NAME"
    value: "{settings.lakebase_endpoint_name}"
"""


def _render_template(template_name: str, context: dict) -> str:
    """Render a Jinja2 template from the resources/templates directory."""
    from pathlib import Path
    from jinja2 import Template

    template_dir = Path(__file__).parent.parent.parent / "resources" / "templates"
    template_path = template_dir / template_name

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    template_content = template_path.read_text(encoding="utf-8")
    template = Template(template_content)
    return template.render(**context)


def _generate_route_from_template(table: dict) -> str:
    """Generate a route file from the entity template using table definition.

    This is 100% deterministic — no LLM involved. The template produces
    the exact same pattern that worked in the deployed simple-notes-app.
    """
    entity_name = table["name"]
    # Pluralize simply (add 's' if not already plural)
    entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"

    columns = []
    for col in table.get("columns", []):
        python_type = _pg_type_to_python(col.get("data_type", "text"))
        columns.append({
            "name": col["name"],
            "python_type": python_type,
            "is_nullable": col.get("is_nullable", True) or col["name"] in ("id", "created_at", "updated_at"),
        })

    # Build column helpers for SQL
    all_col_names = [c["name"] for c in columns]
    insert_cols = [c["name"] for c in columns if c["name"] not in ("id", "created_at", "updated_at")]

    column_names = ", ".join(all_col_names)
    insert_columns = ", ".join(insert_cols)
    insert_placeholders = ", ".join(["%s"] * len(insert_cols))
    insert_values = ", ".join([f"item.{c}" for c in insert_cols])

    return _render_template("routes_entity.py.j2", {
        "entity_name": entity_name,
        "entity_plural": entity_plural,
        "columns": columns,
        "column_names": column_names,
        "insert_columns": insert_columns,
        "insert_placeholders": insert_placeholders,
        "insert_values": insert_values,
    })


def _summarize_data_model(data_model: dict) -> str:
    tables = data_model.get("tables", [])
    parts = []
    for table in tables:
        cols = []
        for c in table.get("columns", [])[:10]:
            nullable = "" if c.get("is_nullable", True) else " NOT NULL"
            cols.append(f"{c['name']} ({c['data_type']}{nullable})")
        fks = [f"{fk['column']}→{fk['references_table']}" for fk in table.get("foreign_keys", [])]
        parts.append(f"Table: {table['name']} | Columns: {', '.join(cols)} | FKs: {', '.join(fks) or 'none'}")
    return "\n".join(parts)


def _pg_type_to_python(pg_type: str) -> str:
    """Convert PostgreSQL data type to Python type hint."""
    pg_type = pg_type.upper()
    mapping = {
        "UUID": "str",
        "TEXT": "str",
        "VARCHAR": "str",
        "CHAR": "str",
        "CHARACTER VARYING": "str",
        "INTEGER": "int",
        "INT": "int",
        "BIGINT": "int",
        "SMALLINT": "int",
        "SERIAL": "int",
        "BOOLEAN": "bool",
        "BOOL": "bool",
        "FLOAT": "float",
        "DOUBLE PRECISION": "float",
        "NUMERIC": "float",
        "DECIMAL": "float",
        "REAL": "float",
        "TIMESTAMP": "datetime",
        "TIMESTAMPTZ": "datetime",
        "TIMESTAMP WITH TIME ZONE": "datetime",
        "TIMESTAMP WITHOUT TIME ZONE": "datetime",
        "DATE": "str",
        "TIME": "str",
        "JSON": "str",
        "JSONB": "str",
    }
    # Check for exact match first
    if pg_type in mapping:
        return mapping[pg_type]
    # Check for partial match (e.g., "VARCHAR(255)")
    for key, value in mapping.items():
        if pg_type.startswith(key):
            return value
    return "str"


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```python"):
        text = text[9:]
    elif text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
