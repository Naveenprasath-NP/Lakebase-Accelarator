"""Prototype Ingestion Service — Step 1 of the brownfield pipeline.

Reverse-engineers uploaded prototype artifacts using Model Serving and extracts
structured entities, relationships, gaps, and migration recommendations.
"""

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.models.analysis import PrototypeAnalysis
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import LLM_RESPONSE_INVALID
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class PrototypeIngestionService:
    """Reverse-engineers a prototype and extracts structured requirements."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    async def execute(
        self,
        prompt: str,
        file_contents: dict[str, str] | None = None,
    ) -> PrototypeAnalysis:
        """Analyze prototype artifacts and return structured analysis.

        Args:
            prompt: User description of the existing prototype.
            file_contents: Dict of filename → content (text files only).
                          Binary files are described by name only.

        Returns:
            PrototypeAnalysis with entities, gaps, and migration notes.

        Raises:
            PipelineStepError: If analysis fails or produces invalid output.
        """
        logger.info(
            "Starting prototype ingestion",
            extra={"step": "prototype_ingestion", "file_count": len(file_contents or {})},
        )

        system_prompt = get_system_prompt("prototype_ingestion")

        # Build user prompt with file context
        user_prompt = self._build_user_prompt(prompt, file_contents)

        result = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=PrototypeAnalysis,
            temperature=0.1,
            max_tokens=8192,
        )

        # Post-validation
        self._validate_result(result)

        logger.info(
            f"Prototype ingestion complete: {len(result.entities)} entities, "
            f"{len(result.production_gaps)} gaps identified",
            extra={"step": "prototype_ingestion", "project_name": result.project_name},
        )
        return result

    def _build_user_prompt(self, prompt: str, file_contents: dict[str, str] | None) -> str:
        """Build the user prompt with file context appended."""
        parts = [f"User description:\n{prompt}"]

        if file_contents:
            parts.append("\n\n--- Prototype Files ---\n")
            for filename, content in file_contents.items():
                # Truncate very large files to avoid token limits
                truncated = content[:5000] if len(content) > 5000 else content
                parts.append(f"\n### File: {filename}\n```\n{truncated}\n```\n")

        return "\n".join(parts)

    def _validate_result(self, result: PrototypeAnalysis) -> None:
        """Validate business rules on the PrototypeAnalysis."""
        if not result.entities:
            raise PipelineStepError(
                step_name="prototype_ingestion",
                message="LLM returned no entities from prototype analysis",
                error_code=LLM_RESPONSE_INVALID,
            )

        for entity in result.entities:
            has_pk = any(attr.is_primary_key for attr in entity.attributes)
            if not has_pk:
                raise PipelineStepError(
                    step_name="prototype_ingestion",
                    message=f"Entity '{entity.name}' has no primary key attribute",
                    error_code=LLM_RESPONSE_INVALID,
                )
