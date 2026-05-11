"""Model Consumption Service — logs LLM token usage to the database.

Records token consumption metrics for every LLM call made during pipeline
execution. Uses fire-and-forget pattern: DB failures are logged but never
raised to avoid blocking pipeline execution.
"""

from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA
from lakebase_accelerator.utils.logger import logger


class ModelConsumptionService:
    """Logs LLM token consumption to accelerator_meta.model_consumption.

    Fire-and-forget: all DB errors are caught and logged via structured
    logger. This service never raises exceptions to callers.
    """

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    def log_consumption(
        self,
        project_id: str,
        model_endpoint: str,
        model_name: str,
        call_type: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: float,
        status: str,
    ) -> None:
        """Log a single LLM call's token consumption to the database.

        This method is fire-and-forget: it will never raise an exception.
        On failure, it falls back to structured logger output.

        Args:
            project_id: UUID of the project this call belongs to.
            model_endpoint: The model serving endpoint name.
            model_name: The specific model name used.
            call_type: Type of LLM call (e.g., 'intake', 'exploration', 'generation').
            input_tokens: Number of input/prompt tokens consumed.
            output_tokens: Number of output/completion tokens generated.
            total_tokens: Total tokens (input + output).
            latency_ms: Round-trip latency in milliseconds.
            status: Call status (e.g., 'success', 'error').
        """
        try:
            self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                INSERT INTO accelerator_meta.model_consumption
                    (project_id, model_endpoint, model_name, call_type,
                     input_tokens, output_tokens, total_tokens, latency_ms, status)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    model_endpoint,
                    model_name,
                    call_type,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    latency_ms,
                    status,
                ),
            )
            logger.info(
                "Model consumption logged",
                extra={
                    "project_id": project_id,
                    "model_name": model_name,
                    "call_type": call_type,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "latency_ms": latency_ms,
                    "status": status,
                },
            )
        except Exception as e:
            # Fire-and-forget: log warning and continue without raising
            logger.warning(
                f"Failed to log model consumption to DB: {e}",
                extra={
                    "project_id": project_id,
                    "model_name": model_name,
                    "call_type": call_type,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "latency_ms": latency_ms,
                    "status": status,
                    "error": str(e),
                },
            )
