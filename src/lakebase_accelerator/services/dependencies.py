"""Dependency injection — wires all services, repositories, and clients.

Uses FastAPI's Depends pattern with factory functions.
The WorkspaceClient and connection pool are initialized at app startup (lifespan).
"""

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.repositories.databricks_apps_repository import DatabricksAppsRepository
from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.repositories.workspace_files_repository import WorkspaceFilesRepository
from lakebase_accelerator.services.app_deployment_service import AppDeploymentService
from lakebase_accelerator.services.audit_service import AuditService
from lakebase_accelerator.services.backend_generation_service import BackendGenerationService
from lakebase_accelerator.services.data_model_inference_service import DataModelInferenceService
from lakebase_accelerator.services.deployment_config_service import DeploymentConfigService
from lakebase_accelerator.services.frontend_generation_service import FrontendGenerationService
from lakebase_accelerator.services.greenfield_pipeline_service import GreenFieldPipelineService
from lakebase_accelerator.services.permission_service import PermissionService
from lakebase_accelerator.services.prototype_ingestion_service import PrototypeIngestionService
from lakebase_accelerator.services.requirement_intake_service import RequirementIntakeService
from lakebase_accelerator.services.schema_provisioning_service import SchemaProvisioningService
from lakebase_accelerator.services.seed_data_service import SeedDataService
from lakebase_accelerator.services.validation_service import ValidationService
from lakebase_accelerator.settings import get_settings

# ─── Singleton holders (initialized in lifespan) ────────────────────

_workspace_client = None
_connection_pool = None


def set_workspace_client(client) -> None:
    """Set the workspace client (called during app startup)."""
    global _workspace_client
    _workspace_client = client


def set_connection_pool(pool) -> None:
    """Set the connection pool (called during app startup)."""
    global _connection_pool
    _connection_pool = pool


def get_workspace_client():
    """Get the initialized workspace client."""
    if _workspace_client is None:
        raise RuntimeError("WorkspaceClient not initialized. Call set_workspace_client() during startup.")
    return _workspace_client


def get_connection_pool():
    """Get the initialized connection pool."""
    if _connection_pool is None:
        raise RuntimeError("Connection pool not initialized. Call set_connection_pool() during startup.")
    return _connection_pool


# ─── Client factories ────────────────────────────────────────────────


def get_llm_client() -> LLMClient:
    """Create LLMClient with direct HTTP config."""
    settings = get_settings()
    return LLMClient(
        databricks_host=settings.databricks_host,
        client_id=settings.databricks_client_id,
        client_secret=settings.databricks_client_secret,
        endpoint_name=settings.model_serving_endpoint,
        max_retries=settings.llm_max_retries,
        timeout_seconds=settings.llm_timeout_seconds,
    )


# ─── Repository factories ───────────────────────────────────────────


def get_lakebase_repository() -> LakebaseRepository:
    """Create LakebaseRepository with connection pool."""
    return LakebaseRepository(connection_pool=get_connection_pool())


def get_workspace_files_repository() -> WorkspaceFilesRepository:
    """Create WorkspaceFilesRepository with workspace client."""
    return WorkspaceFilesRepository(workspace_client=get_workspace_client())


def get_databricks_apps_repository() -> DatabricksAppsRepository:
    """Create DatabricksAppsRepository with workspace client."""
    settings = get_settings()
    return DatabricksAppsRepository(
        workspace_client=get_workspace_client(),
        base_url=settings.databricks_host,
    )


# ─── Service factories ──────────────────────────────────────────────


def get_requirement_intake_service() -> RequirementIntakeService:
    return RequirementIntakeService(llm_client=get_llm_client())


def get_prototype_ingestion_service() -> PrototypeIngestionService:
    return PrototypeIngestionService(llm_client=get_llm_client())


def get_data_model_inference_service() -> DataModelInferenceService:
    return DataModelInferenceService(llm_client=get_llm_client())


def get_schema_provisioning_service() -> SchemaProvisioningService:
    return SchemaProvisioningService(lakebase_repo=get_lakebase_repository())


def get_seed_data_service() -> SeedDataService:
    return SeedDataService(llm_client=get_llm_client(), lakebase_repo=get_lakebase_repository())


def get_frontend_generation_service() -> FrontendGenerationService:
    return FrontendGenerationService(llm_client=get_llm_client())


def get_backend_generation_service() -> BackendGenerationService:
    return BackendGenerationService(llm_client=get_llm_client())


def get_deployment_config_service() -> DeploymentConfigService:
    return DeploymentConfigService()


def get_validation_service() -> ValidationService:
    return ValidationService(lakebase_repo=get_lakebase_repository())


def get_app_deployment_service() -> AppDeploymentService:
    return AppDeploymentService(
        workspace_repo=get_workspace_files_repository(),
        apps_repo=get_databricks_apps_repository(),
    )


def get_permission_service() -> PermissionService:
    return PermissionService(
        apps_repo=get_databricks_apps_repository(),
        lakebase_repo=get_lakebase_repository(),
    )


def get_audit_service() -> AuditService:
    return AuditService(lakebase_repo=get_lakebase_repository())


# ─── Pipeline factory ────────────────────────────────────────────────


def get_greenfield_pipeline_service() -> GreenFieldPipelineService:
    """Create the fully-wired greenfield pipeline service."""
    return GreenFieldPipelineService(
        requirement_intake=get_requirement_intake_service(),
        data_model_inference=get_data_model_inference_service(),
        schema_provisioning=get_schema_provisioning_service(),
        seed_data=get_seed_data_service(),
        frontend_generation=get_frontend_generation_service(),
        backend_generation=get_backend_generation_service(),
        deployment_config=get_deployment_config_service(),
        validation=get_validation_service(),
        app_deployment=get_app_deployment_service(),
        permission=get_permission_service(),
        audit=get_audit_service(),
    )
