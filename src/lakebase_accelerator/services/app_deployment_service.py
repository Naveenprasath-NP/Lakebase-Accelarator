"""App Deployment Service — Steps 9-10 of both pipelines.

Writes generated files to Databricks Workspace and creates/deploys
a Databricks App via the REST API.
"""

from lakebase_accelerator.models.deployment import AppResource, DeploymentStatus
from lakebase_accelerator.repositories.databricks_apps_repository import DatabricksAppsRepository
from lakebase_accelerator.repositories.workspace_files_repository import WorkspaceFilesRepository
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger


class AppDeploymentService:
    """Orchestrates workspace file writing and Databricks App deployment."""

    def __init__(
        self,
        workspace_repo: WorkspaceFilesRepository,
        apps_repo: DatabricksAppsRepository,
    ) -> None:
        self._workspace_repo = workspace_repo
        self._apps_repo = apps_repo

    async def write_files(self, app_name: str, files: dict[str, str]) -> str:
        """Write all generated files to Databricks Workspace.

        Args:
            app_name: App name (used as directory name).
            files: Dict of relative_path → content.

        Returns:
            Workspace path where files were written.
        """
        logger.info(
            f"Writing {len(files)} files to workspace",
            extra={"step": "workspace_write", "app_name": app_name},
        )
        workspace_path = await self._workspace_repo.write_files(app_name, files)
        logger.info(
            f"Workspace write complete: {workspace_path}",
            extra={"step": "workspace_write"},
        )
        return workspace_path

    async def deploy(self, app_name: str, workspace_path: str, schema_name: str) -> DeploymentStatus:
        """Create and deploy a Databricks App.

        Args:
            app_name: Name for the new Databricks App.
            workspace_path: Path to source files in workspace.
            schema_name: Lakebase schema for resource attachment.

        Returns:
            DeploymentStatus with app URL.

        Raises:
            AppDeploymentError: If creation, deployment, or polling fails.
        """
        settings = get_settings()

        logger.info(
            f"Deploying Databricks App: {app_name}",
            extra={"step": "app_deployment", "app_name": app_name},
        )

        # Create the app with Lakebase resource
        resources = [
            AppResource(
                name="lakebase",
                resource_type="sql_warehouse",
                config={"schema": schema_name},
            )
        ]

        await self._apps_repo.create_app(
            app_name=app_name,
            description=f"Generated app: {app_name}",
            resources=resources,
        )

        # Create deployment
        deployment_info = await self._apps_repo.create_deployment(
            app_name=app_name,
            source_path=workspace_path,
        )

        # Poll until ready
        status = await self._apps_repo.poll_deployment_status(
            app_name=app_name,
            deployment_id=deployment_info.deployment_id,
            poll_interval=settings.deployment_poll_interval_seconds,
            timeout_seconds=settings.deployment_timeout_seconds,
        )

        logger.info(
            f"App deployment complete: {status.app_url}",
            extra={"step": "app_deployment", "app_url": status.app_url},
        )
        return status
