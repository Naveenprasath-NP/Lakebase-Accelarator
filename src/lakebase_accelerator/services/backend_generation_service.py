"""Backend Generation Service — Step 6 of both pipelines.

Generates a FastAPI backend with CRUD endpoints for each entity,
database connection module, and Pydantic models using Model Serving.
"""

import json

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.models.generated_files import GeneratedFiles
from lakebase_accelerator.settings import REQUIRED_BACKEND_FILES
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import BACKEND_GENERATION_FAILED, MISSING_REQUIRED_FILES
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class BackendGenerationService:
    """Generates FastAPI backend code for the project."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    async def execute(self, data_model: DataModel, schema_name: str, project_name: str) -> GeneratedFiles:
        """Generate FastAPI backend source files.

        Args:
            data_model: Data model with table definitions.
            schema_name: Lakebase schema name for the generated app.
            project_name: Project name for display purposes.

        Returns:
            GeneratedFiles with backend source code.

        Raises:
            PipelineStepError: If generation fails or required files are missing.
        """
        logger.info(
            "Starting backend generation",
            extra={"step": "backend_generation", "table_count": len(data_model.tables)},
        )

        system_prompt = get_system_prompt("backend_generation")
        user_prompt = self._build_user_prompt(data_model, schema_name, project_name)

        result = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=None,  # Raw dict — file paths as keys
            temperature=0.2,
            max_tokens=16384,
        )

        if not isinstance(result, dict):
            raise PipelineStepError(
                step_name="backend_generation",
                message="LLM returned non-dict response for backend files",
                error_code=BACKEND_GENERATION_FAILED,
            )

        # Validate required files are present
        missing = [f for f in REQUIRED_BACKEND_FILES if f not in result]
        if missing:
            raise PipelineStepError(
                step_name="backend_generation",
                message=f"Missing required backend files: {missing}",
                error_code=MISSING_REQUIRED_FILES,
                details={"missing_files": missing},
            )

        files = GeneratedFiles(files=result)

        logger.info(
            f"Backend generation complete: {len(files.files)} files",
            extra={"step": "backend_generation"},
        )
        return files

    def _build_user_prompt(self, data_model: DataModel, schema_name: str, project_name: str) -> str:
        """Build user prompt with full table definitions for backend generation."""
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
                "project_name": project_name,
                "schema_name": schema_name,
                "tables": tables_info,
                "creation_order": data_model.creation_order,
            },
            indent=2,
        )
