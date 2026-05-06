"""Requirement Intake Service — Step 1 of the greenfield pipeline.

Analyzes a natural-language business prompt using Model Serving and extracts
structured entities, relationships, and workflows as an AnalysisResult.
"""

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.models.analysis import AnalysisResult
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import LLM_RESPONSE_INVALID
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class RequirementIntakeService:
    """Analyzes a business prompt and extracts structured requirements."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    async def execute(self, prompt: str) -> AnalysisResult:
        """Analyze the user's business prompt and return structured entities.

        Args:
            prompt: Natural-language business prompt from the user.

        Returns:
            AnalysisResult with entities, relationships, workflows.

        Raises:
            PipelineStepError: If analysis fails or produces invalid output.
        """
        logger.info("Starting requirement intake", extra={"step": "requirement_intake"})

        system_prompt = get_system_prompt("requirement_intake")

        result = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=prompt,
            response_model=AnalysisResult,
            temperature=0.1,
            max_tokens=4096,
        )

        # Post-validation: ensure at least one entity with a PK
        self._validate_result(result)

        logger.info(
            f"Requirement intake complete: {len(result.entities)} entities, "
            f"{len(result.relationships)} relationships",
            extra={"step": "requirement_intake", "project_name": result.project_name},
        )
        return result

    def _validate_result(self, result: AnalysisResult) -> None:
        """Validate business rules on the AnalysisResult."""
        if not result.entities and not result.clarification_questions:
            raise PipelineStepError(
                step_name="requirement_intake",
                message="LLM returned no entities and no clarification questions",
                error_code=LLM_RESPONSE_INVALID,
            )

        for entity in result.entities:
            has_pk = any(attr.is_primary_key for attr in entity.attributes)
            if not has_pk:
                raise PipelineStepError(
                    step_name="requirement_intake",
                    message=f"Entity '{entity.name}' has no primary key attribute",
                    error_code=LLM_RESPONSE_INVALID,
                )

        # Validate relationship references
        entity_names = {e.name for e in result.entities}
        for rel in result.relationships:
            if rel.from_entity not in entity_names:
                raise PipelineStepError(
                    step_name="requirement_intake",
                    message=f"Relationship references unknown entity '{rel.from_entity}'",
                    error_code=LLM_RESPONSE_INVALID,
                )
            if rel.to_entity not in entity_names:
                raise PipelineStepError(
                    step_name="requirement_intake",
                    message=f"Relationship references unknown entity '{rel.to_entity}'",
                    error_code=LLM_RESPONSE_INVALID,
                )
