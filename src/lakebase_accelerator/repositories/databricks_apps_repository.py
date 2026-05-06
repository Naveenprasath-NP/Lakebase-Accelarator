"""Repository for Databricks Apps REST API operations."""

import asyncio

import httpx

from lakebase_accelerator.models.deployment import AppInfo, AppResource, DeploymentInfo, DeploymentStatus
from lakebase_accelerator.settings import BACKOFF_BASE_SECONDS, MAX_DEPLOYMENT_RETRIES
from lakebase_accelerator.utils.exceptions import AppDeploymentError
from lakebase_accelerator.utils.logger import logger


class DatabricksAppsRepository:
    """Repository for Databricks Apps REST API operations.

    Uses httpx.AsyncClient for HTTP calls. Authenticates using workspace_client token.
    Retries on 5xx with exponential backoff (max 3 retries).
    Raises AppDeploymentError on 4xx or timeout.
    """

    def __init__(self, workspace_client, base_url: str) -> None:
        self._workspace_client = workspace_client
        self._base_url = base_url.rstrip("/")

    def _get_headers(self) -> dict[str, str]:
        """Get authentication headers from workspace client."""
        token = self._workspace_client.config.token
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        json_body: dict | None = None,
    ) -> dict:
        """Execute an HTTP request with retry on 5xx errors.

        Retries up to MAX_DEPLOYMENT_RETRIES times with exponential backoff.
        Raises AppDeploymentError on 4xx or after exhausting retries.
        """
        headers = self._get_headers()
        last_error: Exception | None = None

        for attempt in range(MAX_DEPLOYMENT_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json_body,
                    )

                if response.status_code >= 500:
                    last_error = AppDeploymentError(f"Server error {response.status_code}: {response.text}")
                    if attempt < MAX_DEPLOYMENT_RETRIES:
                        wait_time = BACKOFF_BASE_SECONDS * (2**attempt)
                        logger.warning(f"Retrying request to {url} after {wait_time}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait_time)
                        continue
                    raise last_error

                if response.status_code >= 400:
                    raise AppDeploymentError(f"Client error {response.status_code}: {response.text}")

                return response.json()

            except httpx.TimeoutException as e:
                last_error = AppDeploymentError(f"Request timeout: {e}")
                if attempt < MAX_DEPLOYMENT_RETRIES:
                    wait_time = BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(f"Retrying after timeout (attempt {attempt + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                raise AppDeploymentError(f"Request timed out after {MAX_DEPLOYMENT_RETRIES} retries") from e

            except AppDeploymentError:
                raise

            except Exception as e:
                raise AppDeploymentError(f"Unexpected error during request: {e}") from e

        raise last_error or AppDeploymentError("Request failed after retries")

    async def create_app(self, app_name: str, description: str, resources: list[AppResource]) -> AppInfo:
        """Create a new Databricks App via POST /api/2.0/apps."""
        url = f"{self._base_url}/api/2.0/apps"
        payload = {
            "name": app_name,
            "description": description,
            "resources": [r.model_dump() for r in resources],
        }

        logger.info(f"Creating Databricks App: {app_name}")
        data = await self._request_with_retry("POST", url, json_body=payload)

        return AppInfo(
            name=data.get("name", app_name),
            url=data.get("url"),
            service_principal_id=data.get("service_principal_id"),
            status=data.get("status", "CREATING"),
        )

    async def create_deployment(self, app_name: str, source_path: str) -> DeploymentInfo:
        """Create a deployment via POST /api/2.0/apps/{name}/deployments."""
        url = f"{self._base_url}/api/2.0/apps/{app_name}/deployments"
        payload = {
            "source_code_path": source_path,
        }

        logger.info(f"Creating deployment for app: {app_name}")
        data = await self._request_with_retry("POST", url, json_body=payload)

        return DeploymentInfo(
            deployment_id=data.get("deployment_id", ""),
            app_name=app_name,
            status=data.get("status", "PENDING"),
            source_path=source_path,
        )

    async def poll_deployment_status(
        self,
        app_name: str,
        deployment_id: str,
        poll_interval: int = 10,
        timeout_seconds: int = 300,
    ) -> DeploymentStatus:
        """Poll deployment status until READY or timeout.

        Polls every poll_interval seconds. Raises AppDeploymentError on timeout or failure.
        """
        url = f"{self._base_url}/api/2.0/apps/{app_name}/deployments/{deployment_id}"
        elapsed = 0

        logger.info(f"Polling deployment status: {app_name}/{deployment_id}")

        while elapsed < timeout_seconds:
            data = await self._request_with_retry("GET", url)
            status = data.get("status", "UNKNOWN")

            if status == "SUCCEEDED" or status == "READY":
                logger.info(f"Deployment ready: {app_name}/{deployment_id}")
                return DeploymentStatus(
                    deployment_id=deployment_id,
                    status=status,
                    app_url=data.get("app_url"),
                )

            if status == "FAILED":
                error_msg = data.get("status_message", "Deployment failed")
                raise AppDeploymentError(f"Deployment failed: {error_msg}")

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise AppDeploymentError(f"Deployment timed out after {timeout_seconds}s for app '{app_name}'")

    async def get_app_service_principal(self, app_name: str) -> str:
        """Discover the auto-created SP for a deployed app."""
        url = f"{self._base_url}/api/2.0/apps/{app_name}"

        logger.info(f"Discovering service principal for app: {app_name}")
        data = await self._request_with_retry("GET", url)

        sp_id = data.get("service_principal_id")
        if not sp_id:
            raise AppDeploymentError(f"No service principal found for app '{app_name}'")

        return sp_id
