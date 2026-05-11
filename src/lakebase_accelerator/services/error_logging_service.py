"""Error Logging Service — logs application errors to the database.

Records error details for every caught exception during pipeline execution.
Uses fire-and-forget pattern: DB failures are logged but never raised to
avoid blocking error handling or pipeline execution.
"""

from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA
from lakebase_accelerator.utils.logger import logger


class ErrorLoggingService:
    """Logs application errors to accelerator_meta.error_logs.

    Fire-and-forget: all DB errors are caught and logged via structured
    logger. This service never raises exceptions to callers.
    """

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    def log_error(
        self,
        project_id: str,
        error_code: str,
        severity: str,
        source_component: str,
        source_step: str,
        error_message: str,
        stack_trace: str | None = None,
        error_context: str | None = None,
        is_retryable: bool = False,
    ) -> None:
        """Log an application error to the database.

        This method is fire-and-forget: it will never raise an exception.
        On failure, it falls back to structured logger output.

        Args:
            project_id: UUID of the project this error belongs to.
            error_code: Short error code (e.g., 'LLM_PARSE_ERROR', 'DB_TIMEOUT').
            severity: Error severity level (e.g., 'critical', 'high', 'medium', 'low').
            source_component: Component where the error originated (e.g., 'intake_node', 'schema_provisioning').
            source_step: Pipeline step where the error occurred (e.g., 'intake', 'data_model').
            error_message: Human-readable error description.
            stack_trace: Full stack trace string (optional).
            error_context: Additional context about the error (optional).
            is_retryable: Whether the operation that failed can be retried.
        """
        try:
            self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                INSERT INTO accelerator_meta.error_logs
                    (project_id, error_code, severity, source_component, source_step,
                     error_message, stack_trace, error_context, is_retryable)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    error_code,
                    severity,
                    source_component,
                    source_step,
                    error_message,
                    stack_trace,
                    error_context,
                    is_retryable,
                ),
            )
            logger.info(
                "Error logged to DB",
                extra={
                    "project_id": project_id,
                    "error_code": error_code,
                    "severity": severity,
                    "source_component": source_component,
                    "source_step": source_step,
                    "is_retryable": is_retryable,
                },
            )
        except Exception as e:
            # Fire-and-forget: log warning and continue without raising
            logger.warning(
                f"Failed to log error to DB: {e}",
                extra={
                    "project_id": project_id,
                    "error_code": error_code,
                    "severity": severity,
                    "source_component": source_component,
                    "source_step": source_step,
                    "error_message": error_message,
                    "is_retryable": is_retryable,
                    "error": str(e),
                },
            )
