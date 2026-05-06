"""Custom exception classes for the Lakebase Accelerator."""


class AcceleratorBaseError(Exception):
    """Base exception for all accelerator errors."""

    def __init__(self, message: str, error_code: str, details: dict | None = None) -> None:
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(self.message)


class PipelineStepError(AcceleratorBaseError):
    """Raised when a pipeline step fails."""

    def __init__(self, step_name: str, message: str, error_code: str, details: dict | None = None) -> None:
        self.step_name = step_name
        super().__init__(
            message=f"Pipeline step '{step_name}' failed: {message}",
            error_code=error_code,
            details={**(details or {}), "step_name": step_name},
        )


class LLMClientError(PipelineStepError):
    """Raised when LLM client calls fail after all retries."""

    def __init__(
        self,
        message: str,
        step_name: str = "llm_call",
        error_code: str = "LLM_MAX_RETRIES_EXCEEDED",
        details: dict | None = None,
    ) -> None:
        super().__init__(step_name=step_name, message=message, error_code=error_code, details=details)


class SchemaProvisioningError(PipelineStepError):
    """Raised when schema or table creation fails."""

    def __init__(self, message: str, error_code: str = "SCHEMA_CREATION_FAILED", details: dict | None = None) -> None:
        super().__init__(step_name="schema_provisioning", message=message, error_code=error_code, details=details)


class SeedDataError(PipelineStepError):
    """Raised when seed data generation or insertion fails."""

    def __init__(self, message: str, error_code: str, details: dict | None = None) -> None:
        super().__init__(step_name="seed_data_generation", message=message, error_code=error_code, details=details)


class AppDeploymentError(PipelineStepError):
    """Raised when app creation or deployment fails."""

    def __init__(self, message: str, error_code: str = "APP_DEPLOYMENT_FAILED", details: dict | None = None) -> None:
        super().__init__(step_name="app_deployment", message=message, error_code=error_code, details=details)


class PermissionGrantError(PipelineStepError):
    """Raised when service principal permission granting fails."""

    def __init__(self, message: str, error_code: str, details: dict | None = None) -> None:
        super().__init__(step_name="permission_grant", message=message, error_code=error_code, details=details)


class ValidationError(PipelineStepError):
    """Raised when pre-deployment validation fails."""

    def __init__(self, message: str, failed_checks: list[str], details: dict | None = None) -> None:
        super().__init__(
            step_name="validation",
            message=message,
            error_code="VALIDATION_FAILED",
            details={**(details or {}), "failed_checks": failed_checks},
        )
        self.failed_checks = failed_checks


class LakebaseConnectionError(AcceleratorBaseError):
    """Raised when Lakebase connection fails."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(
            message=message,
            error_code="DB_CONNECTION_FAILED",
            details=details,
        )
