"""Frontend Generation Service — Step 5 of both pipelines.

Generates a React (Vite + TypeScript + Tailwind) frontend tailored to the
project's data model using Model Serving.
"""

import json

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.models.generated_files import GeneratedFiles
from lakebase_accelerator.settings import REQUIRED_FRONTEND_FILES
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import FRONTEND_GENERATION_FAILED, MISSING_REQUIRED_FILES
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class FrontendGenerationService:
    """Generates React frontend code for the project."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    async def execute(self, data_model: DataModel, project_name: str) -> GeneratedFiles:
        """Generate React frontend source files.

        Args:
            data_model: Data model with table definitions.
            project_name: Project name for display purposes.

        Returns:
            GeneratedFiles with frontend source code.

        Raises:
            PipelineStepError: If generation fails or required files are missing.
        """
        logger.info(
            "Starting frontend generation",
            extra={"step": "frontend_generation", "table_count": len(data_model.tables)},
        )

        system_prompt = get_system_prompt("frontend_generation")
        user_prompt = self._build_user_prompt(data_model, project_name)

        result = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=None,  # Raw dict — file paths as keys
            temperature=0.2,
            max_tokens=16384,
        )

        if not isinstance(result, dict):
            raise PipelineStepError(
                step_name="frontend_generation",
                message="LLM returned non-dict response for frontend files",
                error_code=FRONTEND_GENERATION_FAILED,
            )

        # Validate required files are present
        missing = [f for f in REQUIRED_FRONTEND_FILES if f not in result]
        if missing:
            raise PipelineStepError(
                step_name="frontend_generation",
                message=f"Missing required frontend files: {missing}",
                error_code=MISSING_REQUIRED_FILES,
                details={"missing_files": missing},
            )

        files = GeneratedFiles(files=result)

        logger.info(
            f"Frontend generation complete: {len(files.files)} files",
            extra={"step": "frontend_generation"},
        )
        return files

    def _build_user_prompt(self, data_model: DataModel, project_name: str) -> str:
        """Build user prompt with entity information for frontend generation."""
        entities = []
        for table in data_model.tables:
            columns = [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "nullable": col.nullable,
                    "is_primary_key": col.is_primary_key,
                }
                for col in table.columns
                if col.name not in ("created_at", "updated_at")  # Skip audit columns from forms
            ]
            entities.append({"table_name": table.name, "columns": columns})

        return json.dumps(
            {"project_name": project_name, "entities": entities},
            indent=2,
        )
