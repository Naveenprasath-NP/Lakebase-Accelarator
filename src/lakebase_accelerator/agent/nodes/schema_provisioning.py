"""Schema Provisioning — Node 3 (deterministic, no LLM).

Creates the Lakebase schema, tables, and indexes.
Verifies creation was successful.
"""

from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.agent.tools import _get_repo
from lakebase_accelerator.core.schema_naming import generate_schema_name
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.settings import SCHEMA_MAX_COLLISION_RETRIES
from lakebase_accelerator.utils.logger import logger


def schema_provisioning_node(state: PipelineState) -> dict:
    """Create Lakebase schema and tables from the data model."""
    logger.info("Node: schema_provisioning — creating schema", extra={"step": "schema_provisioning"})

    repo = _get_repo()
    data_model_dict = state["data_model"]
    project_name = state["project_name"]

    # Validate the data model can be parsed
    data_model = DataModel.model_validate(data_model_dict)

    # Generate unique schema name with collision retry
    schema_name = None
    for _ in range(SCHEMA_MAX_COLLISION_RETRIES):
        candidate = generate_schema_name(project_name)
        if not repo.schema_exists(candidate):
            repo.create_schema(candidate)
            schema_name = candidate
            break
        logger.warning(f"Schema name collision: {candidate}")

    if not schema_name:
        return {
            "error": f"Schema name collision after {SCHEMA_MAX_COLLISION_RETRIES} attempts",
            "current_step": "schema_provisioning",
        }

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

    # Verify
    for table_name in table_names:
        if not repo.table_exists(schema_name, table_name):
            return {
                "error": f"Table '{table_name}' was not created successfully",
                "current_step": "schema_provisioning",
            }

    logger.info(
        f"Schema provisioning complete: {schema_name} with {len(table_names)} tables",
        extra={"step": "schema_provisioning", "schema_name": schema_name},
    )

    return {
        "schema_name": schema_name,
        "table_names": table_names,
        "current_step": "schema_provisioning",
        "completed_steps": state.get("completed_steps", []) + ["schema_provisioning"],
    }
