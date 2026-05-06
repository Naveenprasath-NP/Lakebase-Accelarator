"""Permission Service — Step 11 of both pipelines.

Discovers the auto-created service principal for the deployed app and
grants schema-scoped permissions (USAGE + CRUD) on the project's Lakebase schema.
"""

from lakebase_accelerator.repositories.databricks_apps_repository import DatabricksAppsRepository
from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.utils.exceptions import PermissionGrantError
from lakebase_accelerator.utils.exceptions.error_codes import PERMISSION_GRANT_FAILED, SP_DISCOVERY_FAILED
from lakebase_accelerator.utils.logger import logger


class PermissionService:
    """Grants schema-scoped permissions to the generated app's service principal."""

    def __init__(
        self,
        apps_repo: DatabricksAppsRepository,
        lakebase_repo: LakebaseRepository,
    ) -> None:
        self._apps_repo = apps_repo
        self._lakebase_repo = lakebase_repo

    async def execute(self, app_name: str, schema_name: str) -> str:
        """Discover SP and grant schema-scoped permissions.

        Args:
            app_name: Deployed Databricks App name.
            schema_name: Lakebase schema to grant access to.

        Returns:
            Service principal ID that was granted access.

        Raises:
            PermissionGrantError: If SP discovery or grant fails.
        """
        logger.info(
            "Starting permission grant",
            extra={"step": "permission_grant", "app_name": app_name, "schema_name": schema_name},
        )

        # Discover the auto-created SP
        try:
            sp_id = await self._apps_repo.get_app_service_principal(app_name)
        except Exception as e:
            raise PermissionGrantError(
                message=f"Failed to discover service principal for app '{app_name}': {e}",
                error_code=SP_DISCOVERY_FAILED,
            ) from e

        # Grant schema-scoped permissions
        try:
            self._lakebase_repo.grant_schema_access(schema_name, sp_id)
        except Exception as e:
            raise PermissionGrantError(
                message=f"Failed to grant permissions on schema '{schema_name}' to SP '{sp_id}': {e}",
                error_code=PERMISSION_GRANT_FAILED,
            ) from e

        logger.info(
            f"Permission grant complete: {sp_id} → {schema_name}",
            extra={"step": "permission_grant", "sp_id": sp_id},
        )
        return sp_id
