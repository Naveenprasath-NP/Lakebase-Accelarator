"""Unit tests for DatabricksAppsRepository with mocked httpx and workspace client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lakebase_accelerator.models.deployment import AppResource
from lakebase_accelerator.repositories.databricks_apps_repository import DatabricksAppsRepository
from lakebase_accelerator.utils.exceptions import AppDeploymentError


@pytest.fixture
def mock_workspace_client():
    """Create a mock workspace client with token."""
    client = MagicMock()
    client.config.token = "test-token-123"
    return client


@pytest.fixture
def repo(mock_workspace_client):
    """Create a DatabricksAppsRepository with mocked client."""
    return DatabricksAppsRepository(
        workspace_client=mock_workspace_client,
        base_url="https://workspace.databricks.com",
    )


class TestCreateApp:
    """Tests for create_app method."""

    @pytest.mark.asyncio
    async def test_create_app_success(self, repo):
        """Test successful app creation returns AppInfo."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "my-app",
            "url": "https://my-app.databricks.com",
            "service_principal_id": "sp-123",
            "status": "CREATING",
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await repo.create_app(
                app_name="my-app",
                description="Test app",
                resources=[AppResource(name="lakebase", resource_type="compute", config={})],
            )

        assert result.name == "my-app"
        assert result.status == "CREATING"

    @pytest.mark.asyncio
    async def test_create_app_4xx_raises_error(self, repo):
        """Test that 4xx response raises AppDeploymentError."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(AppDeploymentError, match="Client error 400"):
                await repo.create_app("my-app", "Test", [])


class TestCreateDeployment:
    """Tests for create_deployment method."""

    @pytest.mark.asyncio
    async def test_create_deployment_success(self, repo):
        """Test successful deployment creation returns DeploymentInfo."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "deployment_id": "dep-456",
            "status": "PENDING",
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await repo.create_deployment("my-app", "/Workspace/Apps/my-app")

        assert result.deployment_id == "dep-456"
        assert result.app_name == "my-app"
        assert result.status == "PENDING"


class TestPollDeploymentStatus:
    """Tests for poll_deployment_status method."""

    @pytest.mark.asyncio
    async def test_poll_until_ready(self, repo):
        """Test polling returns DeploymentStatus when READY."""
        responses = [
            {"status": "IN_PROGRESS"},
            {"status": "SUCCEEDED", "app_url": "https://my-app.databricks.com"},
        ]
        call_count = [0]

        mock_response = MagicMock()
        mock_response.status_code = 200

        def side_effect(*args, **kwargs):
            idx = min(call_count[0], len(responses) - 1)
            mock_response.json.return_value = responses[idx]
            call_count[0] += 1
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(side_effect=side_effect)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await repo.poll_deployment_status("my-app", "dep-456", poll_interval=1, timeout_seconds=10)

        assert result.status == "SUCCEEDED"
        assert result.app_url == "https://my-app.databricks.com"

    @pytest.mark.asyncio
    async def test_poll_timeout_raises_error(self, repo):
        """Test that timeout raises AppDeploymentError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "IN_PROGRESS"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(AppDeploymentError, match="timed out"):
                await repo.poll_deployment_status("my-app", "dep-456", poll_interval=1, timeout_seconds=3)

    @pytest.mark.asyncio
    async def test_poll_failed_status_raises_error(self, repo):
        """Test that FAILED status raises AppDeploymentError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "FAILED", "status_message": "Out of memory"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(AppDeploymentError, match="Deployment failed"):
                await repo.poll_deployment_status("my-app", "dep-456", poll_interval=1, timeout_seconds=10)


class TestRetryOn5xx:
    """Tests for 5xx retry with exponential backoff."""

    @pytest.mark.asyncio
    async def test_retry_on_5xx_then_success(self, repo):
        """Test that 5xx triggers retry and eventual success."""
        error_response = MagicMock()
        error_response.status_code = 503
        error_response.text = "Service Unavailable"

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"name": "my-app", "status": "ACTIVE"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(side_effect=[error_response, success_response])
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await repo._request_with_retry("GET", "https://workspace.databricks.com/api/2.0/apps/my-app")

        assert result == {"name": "my-app", "status": "ACTIVE"}
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_error(self, repo):
        """Test that exhausting retries raises AppDeploymentError."""
        error_response = MagicMock()
        error_response.status_code = 500
        error_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=error_response)
            mock_client_cls.return_value = mock_client

            with (
                patch("asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(AppDeploymentError, match="Server error 500"),
            ):
                await repo._request_with_retry("GET", "https://workspace.databricks.com/api/2.0/apps/my-app")


class TestGetAppServicePrincipal:
    """Tests for get_app_service_principal method."""

    @pytest.mark.asyncio
    async def test_get_sp_success(self, repo):
        """Test successful SP discovery."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "my-app",
            "service_principal_id": "sp-abc-123",
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await repo.get_app_service_principal("my-app")

        assert result == "sp-abc-123"

    @pytest.mark.asyncio
    async def test_get_sp_not_found_raises_error(self, repo):
        """Test that missing SP raises AppDeploymentError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "my-app",
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(AppDeploymentError, match="No service principal found"):
                await repo.get_app_service_principal("my-app")
