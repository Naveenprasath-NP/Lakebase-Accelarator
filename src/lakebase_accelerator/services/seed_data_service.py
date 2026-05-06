"""Seed Data Service — Step 4 of both pipelines.

Generates domain-appropriate seed data using Model Serving and inserts it
into Lakebase tables in topological order respecting referential integrity.
"""

import json

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.models.seed_data import SeedData
from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import MIN_SEED_ROWS_PER_TABLE
from lakebase_accelerator.utils.exceptions import SeedDataError
from lakebase_accelerator.utils.exceptions.error_codes import (
    SEED_DATA_CONSTRAINT_VIOLATION,
    SEED_DATA_INSERTION_FAILED,
)
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class SeedDataService:
    """Generates and inserts seed data into Lakebase tables."""

    def __init__(self, llm_client: LLMClient, lakebase_repo: LakebaseRepository) -> None:
        self._llm_client = llm_client
        self._repo = lakebase_repo

    async def execute(self, schema_name: str, data_model: DataModel) -> dict[str, int]:
        """Generate seed data and insert into tables.

        Args:
            schema_name: Target Lakebase schema.
            data_model: Data model with table definitions.

        Returns:
            Dict of table_name → row count inserted.

        Raises:
            SeedDataError: If generation or insertion fails.
        """
        logger.info(
            "Starting seed data generation",
            extra={"step": "seed_data_generation", "schema_name": schema_name},
        )

        system_prompt = get_system_prompt("seed_data_generation")
        user_prompt = self._build_user_prompt(data_model)

        seed_data = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=SeedData,
            temperature=0.3,
            max_tokens=16384,
        )

        # Insert data in topological order
        row_counts = {}
        for table_data in seed_data.tables:
            try:
                count = self._repo.insert_seed_data(schema_name, table_data.table_name, table_data.rows)
                row_counts[table_data.table_name] = count
            except Exception as e:
                error_msg = str(e)
                # On data errors (invalid UUID, constraint violation), skip the table and continue
                if any(
                    keyword in error_msg.lower()
                    for keyword in ("violates", "constraint", "invalid input", "syntax")
                ):
                    logger.warning(
                        f"Data error inserting into {table_data.table_name}, skipping",
                        extra={"step": "seed_data_generation", "error": error_msg[:200]},
                    )
                    row_counts[table_data.table_name] = 0
                else:
                    raise SeedDataError(
                        message=f"Failed to insert seed data into '{table_data.table_name}': {error_msg[:200]}",
                        error_code=SEED_DATA_INSERTION_FAILED,
                    ) from e

        logger.info(
            f"Seed data insertion complete: {sum(row_counts.values())} total rows",
            extra={"step": "seed_data_generation", "row_counts": row_counts},
        )
        return row_counts

    def _build_user_prompt(self, data_model: DataModel) -> str:
        """Build user prompt with table definitions for seed data generation."""
        tables_info = []
        for table in data_model.tables:
            columns = [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "nullable": col.nullable,
                    "is_primary_key": col.is_primary_key,
                    "default_expression": col.default_expression,
                }
                for col in table.columns
            ]
            fks = [
                {
                    "column": fk.column,
                    "references_table": fk.references_table,
                    "references_column": fk.references_column,
                }
                for fk in table.foreign_keys
            ]
            tables_info.append({"name": table.name, "columns": columns, "foreign_keys": fks})

        return json.dumps(
            {
                "tables": tables_info,
                "creation_order": data_model.creation_order,
                "min_rows_per_table": MIN_SEED_ROWS_PER_TABLE,
            },
            indent=2,
        )
