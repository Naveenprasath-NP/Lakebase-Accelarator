"""Data Model Inference Service — Step 2 of both pipelines.

Converts an AnalysisResult or PrototypeAnalysis into a DDL-ready DataModel
with tables, columns, foreign keys, indexes, and topological creation order.
"""

import json

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.core.topological_sort import compute_creation_order
from lakebase_accelerator.models.analysis import AnalysisResult, PrototypeAnalysis
from lakebase_accelerator.models.data_model import DataModel
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import LLM_RESPONSE_INVALID
from lakebase_accelerator.utils.logger import logger
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


class DataModelInferenceService:
    """Converts entity analysis into a PostgreSQL data model."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    async def execute(self, analysis: AnalysisResult | PrototypeAnalysis) -> DataModel:
        """Generate a DDL-ready data model from the analysis result.

        Args:
            analysis: AnalysisResult (greenfield) or PrototypeAnalysis (brownfield).

        Returns:
            DataModel with tables in topological creation order.

        Raises:
            PipelineStepError: If model generation fails or has circular dependencies.
        """
        logger.info(
            "Starting data model inference",
            extra={"step": "data_model_inference", "entity_count": len(analysis.entities)},
        )

        system_prompt = get_system_prompt("data_model_inference")

        # Build user prompt from analysis
        user_prompt = self._build_user_prompt(analysis)

        result = await self._llm_client.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=DataModel,
            temperature=0.1,
            max_tokens=8192,
        )

        # Verify/recompute topological order
        verified_order = compute_creation_order(result.tables)

        # Rebuild with verified order
        result = DataModel(tables=result.tables, creation_order=verified_order)

        # Post-validation
        self._validate_result(result)

        logger.info(
            f"Data model inference complete: {len(result.tables)} tables",
            extra={"step": "data_model_inference", "tables": result.creation_order},
        )
        return result

    def _build_user_prompt(self, analysis: AnalysisResult | PrototypeAnalysis) -> str:
        """Build user prompt from analysis result."""
        # Serialize entities and relationships for the LLM
        entities_data = []
        for entity in analysis.entities:
            attrs = [
                {
                    "name": a.name,
                    "data_type": a.data_type.value if hasattr(a.data_type, "value") else str(a.data_type),
                    "nullable": a.nullable,
                    "is_primary_key": a.is_primary_key,
                    "default_value": a.default_value,
                    "max_length": a.max_length,
                }
                for a in entity.attributes
            ]
            entities_data.append({"name": entity.name, "description": entity.description, "attributes": attrs})

        relationships_data = [
            {
                "from_entity": r.from_entity,
                "to_entity": r.to_entity,
                "cardinality": r.cardinality.value if hasattr(r.cardinality, "value") else str(r.cardinality),
                "foreign_key_column": r.foreign_key_column,
                "on_delete": r.on_delete,
            }
            for r in analysis.relationships
        ]

        return json.dumps(
            {"entities": entities_data, "relationships": relationships_data},
            indent=2,
        )

    def _validate_result(self, result: DataModel) -> None:
        """Validate the generated data model."""
        for table in result.tables:
            pk_count = sum(1 for col in table.columns if col.is_primary_key)
            if pk_count == 0:
                raise PipelineStepError(
                    step_name="data_model_inference",
                    message=f"Table '{table.name}' has no primary key column",
                    error_code=LLM_RESPONSE_INVALID,
                )
            if pk_count > 1:
                raise PipelineStepError(
                    step_name="data_model_inference",
                    message=f"Table '{table.name}' has {pk_count} primary key columns (expected 1)",
                    error_code=LLM_RESPONSE_INVALID,
                )
