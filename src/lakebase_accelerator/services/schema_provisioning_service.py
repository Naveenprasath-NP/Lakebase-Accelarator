"""Schema Provisioning Service — Step 3 of both pipelines.

Creates a dedicated Lakebase schema and tables for the project.
Handles naming collisions with retry and executes DDL in topological order.
"""

from lakebase_accelerator.core.schema_naming import generate_schema_name
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import SCHEMA_MAX_COLLISION_RETRIES
from lakebase_accelerator.utils.exceptions import SchemaProvisioningError
from lakebase_accelerator.utils.exceptions.error_codes import SCHEMA_NAME_COLLISION
from lakebase_accelerator.utils.logger import logger


class SchemaProvisioningService:
    """Creates Lakebase schema and tables for a project."""

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    async def execute(self, data_model: DataModel, project_name: str) -> tuple[str, list[str]]:
        """Create schema and tables in Lakebase.

        Args:
            data_model: DDL-ready data model with tables in creation order.
            project_name: Human-readable project name for schema naming.

        Returns:
            Tuple of (schema_name, list of created table names).

        Raises:
            SchemaProvisioningError: If schema/table creation fails after retries.
        """
        logger.info(
            "Starting schema provisioning",
            extra={"step": "schema_provisioning", "project_name": project_name},
        )

        # Generate schema name with collision retry
        schema_name = self._create_schema_with_retry(project_name)

        # Create tables in topological order
        ordered_tables = self._get_tables_in_order(data_model)
        table_names = self._repo.create_tables(schema_name, ordered_tables)

        # Create indexes
        all_indexes = []
        for table in data_model.tables:
            all_indexes.extend(table.indexes)
        if all_indexes:
            self._repo.create_indexes(schema_name, all_indexes)

        logger.info(
            f"Schema provisioning complete: {schema_name} with {len(table_names)} tables",
            extra={"step": "schema_provisioning", "schema_name": schema_name},
        )
        return schema_name, table_names

    def _create_schema_with_retry(self, project_name: str) -> str:
        """Generate schema name and create it, retrying on collision."""
        for attempt in range(SCHEMA_MAX_COLLISION_RETRIES):
            schema_name = generate_schema_name(project_name)
            if not self._repo.schema_exists(schema_name):
                self._repo.create_schema(schema_name)
                return schema_name
            logger.warning(
                f"Schema name collision: {schema_name}, retrying (attempt {attempt + 1})",
                extra={"step": "schema_provisioning"},
            )

        raise SchemaProvisioningError(
            message=f"Schema name collision after {SCHEMA_MAX_COLLISION_RETRIES} attempts",
            error_code=SCHEMA_NAME_COLLISION,
        )

    def _get_tables_in_order(self, data_model: DataModel) -> list:
        """Return tables sorted by creation_order."""
        order_map = {name: idx for idx, name in enumerate(data_model.creation_order)}
        return sorted(data_model.tables, key=lambda t: order_map.get(t.name, 999))
