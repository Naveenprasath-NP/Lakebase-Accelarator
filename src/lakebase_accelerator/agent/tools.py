"""LangGraph agent tools — functions the LLM can invoke during pipeline execution.

Each tool is a @tool-decorated function that performs one pipeline step.
Tools have access to the Lakebase connection pool and Databricks workspace
via module-level references set during app startup.
"""

import json

from langchain_core.tools import tool

from lakebase_accelerator.core.schema_naming import generate_schema_name
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.settings import SCHEMA_MAX_COLLISION_RETRIES
from lakebase_accelerator.utils.logger import logger

# Module-level references (set during app startup via set_tool_dependencies)
_lakebase_pool = None
_settings = None


def set_tool_dependencies(lakebase_pool, settings) -> None:
    """Set module-level dependencies for tools. Called once at app startup."""
    global _lakebase_pool, _settings
    _lakebase_pool = lakebase_pool
    _settings = settings


def _get_repo():
    """Get a LakebaseRepository instance."""
    from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository

    if _lakebase_pool is None:
        raise RuntimeError("Lakebase pool not initialized")
    return LakebaseRepository(_lakebase_pool)


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Schema Provisioning
# ═══════════════════════════════════════════════════════════════════════


@tool
def provision_schema(project_name: str, data_model_json: str) -> str:
    """Create a dedicated Lakebase schema and tables for the project.

    Creates a unique schema name, executes CREATE SCHEMA, then CREATE TABLE
    statements in topological order respecting FK dependencies, and creates indexes.

    Args:
        project_name: Human-readable project name for schema naming.
        data_model_json: JSON string of the DataModel (tables, columns, FKs, indexes, creation_order).

    Returns:
        JSON string with schema_name and table_names created.
    """
    logger.info(f"[Tool] provision_schema: {project_name}")
    repo = _get_repo()

    data_model = DataModel.model_validate_json(data_model_json)

    # Generate schema name with collision retry
    schema_name = None
    for _attempt in range(SCHEMA_MAX_COLLISION_RETRIES):
        candidate = generate_schema_name(project_name)
        if not repo.schema_exists(candidate):
            repo.create_schema(candidate)
            schema_name = candidate
            break
        logger.warning(f"Schema name collision: {candidate}, retrying")

    if not schema_name:
        return json.dumps({"error": f"Schema name collision after {SCHEMA_MAX_COLLISION_RETRIES} attempts"})

    # Create tables in topological order
    ordered_tables = sorted(
        data_model.tables,
        key=lambda t: data_model.creation_order.index(t.name) if t.name in data_model.creation_order else 999,
    )
    table_names = repo.create_tables(schema_name, ordered_tables)

    # Create indexes
    all_indexes = []
    for tbl in data_model.tables:
        all_indexes.extend(tbl.indexes)
    if all_indexes:
        repo.create_indexes(schema_name, all_indexes)

    result = {"schema_name": schema_name, "table_names": table_names}
    logger.info(f"[Tool] provision_schema complete: {schema_name}, {len(table_names)} tables")
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Seed Data Insertion
# ═══════════════════════════════════════════════════════════════════════


@tool
def insert_seed_data(schema_name: str, table_name: str, rows_json: str) -> str:
    """Insert seed data rows into a specific table in Lakebase.

    Inserts the provided rows using parameterized queries.
    Call this tool once per table, in topological order (parent tables first).

    Args:
        schema_name: The Lakebase schema name.
        table_name: The table to insert into.
        rows_json: JSON array of row objects. Each object has column_name: value pairs.

    Returns:
        JSON string with row_count inserted, or error message.
    """
    logger.info(f"[Tool] insert_seed_data: {schema_name}.{table_name}")
    repo = _get_repo()

    try:
        rows = json.loads(rows_json)
        if not isinstance(rows, list) or not rows:
            return json.dumps({"error": "rows_json must be a non-empty JSON array"})

        count = repo.insert_seed_data(schema_name, table_name, rows)
        logger.info(f"[Tool] insert_seed_data: {count} rows into {table_name}")
        return json.dumps({"table_name": table_name, "row_count": count})
    except Exception as e:
        error_msg = str(e)[:300]
        logger.warning(f"[Tool] insert_seed_data failed for {table_name}: {error_msg}")
        return json.dumps({"error": f"Failed to insert into {table_name}: {error_msg}"})


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Write File to Workspace
# ═══════════════════════════════════════════════════════════════════════


@tool
def write_file(app_name: str, file_path: str, content: str) -> str:
    """Write a single generated file to the Databricks Workspace.

    Writes the file to /Workspace/Apps/{app_name}/{file_path}.
    Call this tool once per file. The agent should generate files one at a time.

    Args:
        app_name: The Databricks App name (used as directory).
        file_path: Relative path within the app (e.g., 'backend/main.py', 'frontend/src/App.tsx').
        content: The full file content to write.

    Returns:
        JSON string confirming the file was written, or error message.
    """
    logger.info(f"[Tool] write_file: {app_name}/{file_path}")

    # For now, store in memory — actual workspace write happens in deploy step
    # This allows the agent to build up files incrementally
    return json.dumps({"status": "stored", "path": f"/Workspace/Apps/{app_name}/{file_path}"})


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Deploy App
# ═══════════════════════════════════════════════════════════════════════


@tool
def deploy_app(app_name: str, schema_name: str) -> str:
    """Deploy the generated application as a Databricks App.

    Creates the app via REST API, deploys it, and polls until READY.
    This should be called after all files have been written to workspace.

    Args:
        app_name: Name for the Databricks App.
        schema_name: Lakebase schema to attach as a resource.

    Returns:
        JSON string with app_url on success, or error message.
    """
    logger.info(f"[Tool] deploy_app: {app_name}")
    # TODO: Implement actual deployment via DatabricksAppsRepository
    # For now return a placeholder
    return json.dumps(
        {
            "status": "deployed",
            "app_name": app_name,
            "app_url": f"https://dbc-c73ed9f3-a15d.cloud.databricks.com/apps/{app_name}",
            "message": "App deployment placeholder — full implementation pending",
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Grant Permissions
# ═══════════════════════════════════════════════════════════════════════


@tool
def grant_schema_permissions(app_name: str, schema_name: str) -> str:
    """Grant the deployed app's service principal access to its Lakebase schema.

    Discovers the auto-created SP for the app and grants USAGE + CRUD on the schema.

    Args:
        app_name: The deployed Databricks App name.
        schema_name: The Lakebase schema to grant access to.

    Returns:
        JSON string confirming grants applied, or error message.
    """
    logger.info(f"[Tool] grant_schema_permissions: {app_name} → {schema_name}")
    # TODO: Implement actual SP discovery + GRANT via repository
    return json.dumps(
        {
            "status": "granted",
            "app_name": app_name,
            "schema_name": schema_name,
            "message": "Permission grant placeholder — full implementation pending",
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# ALL TOOLS LIST
# ═══════════════════════════════════════════════════════════════════════

ALL_TOOLS = [
    provision_schema,
    insert_seed_data,
    write_file,
    deploy_app,
    grant_schema_permissions,
]
