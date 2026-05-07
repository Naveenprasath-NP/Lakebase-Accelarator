"""Unit tests for WorkspaceFilesRepository with mocked workspace client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lakebase_accelerator.repositories.workspace_files_repository import WorkspaceFilesRepository
from lakebase_accelerator.utils.exceptions import PipelineStepError


@pytest.fixture
def mock_workspace_client():
    """Create a mock workspace client."""
    client = MagicMock()
    client.workspace.mkdirs = MagicMock()
    client.workspace.import_ = MagicMock()
    client.workspace.get_status = MagicMock()
    return client


@pytest.fixture
def repo(mock_workspace_client):
    """Create a WorkspaceFilesRepository with mocked client."""
    return WorkspaceFilesRepository(workspace_client=mock_workspace_client)


class TestWriteFiles:
    """Tests for write_files method."""

    @pytest.mark.asyncio
    async def test_write_files_success(self, repo, mock_workspace_client):
        """Test successful file writing returns workspace path."""
        files = {
            "src/main.py": "print('hello')",
            "requirements.txt": "fastapi==0.115.0",
        }

        async def mock_to_thread(func, *args, **kwargs):
            return None

        with patch("asyncio.to_thread", side_effect=mock_to_thread):
            result = await repo.write_files("my-app", files)

        assert result == "/Workspace/Apps/my-app"

    @pytest.mark.asyncio
    async def test_write_files_creates_directories(self, repo, mock_workspace_client):
        """Test that directory structure is created for nested files."""
        files = {
            "src/components/App.tsx": "export default App;",
        }

        call_args = []

        async def track_to_thread(func, *args, **kwargs):
            call_args.append((func, args))
            return None

        with patch("asyncio.to_thread", side_effect=track_to_thread):
            await repo.write_files("my-app", files)

        # Should have calls for mkdirs and import_
        mkdirs_calls = [c for c in call_args if c[0] == mock_workspace_client.workspace.mkdirs]
        assert len(mkdirs_calls) >= 1

    @pytest.mark.asyncio
    async def test_write_files_retry_on_transient_error(self, repo, mock_workspace_client):
        """Test that transient errors trigger retry with backoff."""
        files = {"main.py": "print('hello')"}

        call_count = [0]

        async def mock_to_thread(func, *args, **kwargs):
            if func == mock_workspace_client.workspace.import_:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("Transient network error")
            return None

        with patch("asyncio.to_thread", side_effect=mock_to_thread), patch("asyncio.sleep", new_callable=AsyncMock):
            await repo.write_files("my-app", files)

        # Should have retried the import_ call
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_write_files_exhausted_retries_raises_error(self, repo, mock_workspace_client):
        """Test that exhausting retries raises PipelineStepError."""
        files = {"main.py": "print('hello')"}

        async def always_fail(func, *args, **kwargs):
            if func == mock_workspace_client.workspace.import_:
                raise Exception("Persistent failure")
            return None

        with (
            patch("asyncio.to_thread", side_effect=always_fail),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(PipelineStepError, match="Failed to write file"),
        ):
            await repo.write_files("my-app", files)


class TestFileExists:
    """Tests for file_exists method."""

    @pytest.mark.asyncio
    async def test_file_exists_returns_true(self, repo, mock_workspace_client):
        """Test returns True when file exists."""

        async def mock_to_thread(func, *args, **kwargs):
            return MagicMock()

        with patch("asyncio.to_thread", side_effect=mock_to_thread):
            result = await repo.file_exists("/Workspace/Apps/my-app/main.py")

        assert result is True

    @pytest.mark.asyncio
    async def test_file_exists_returns_false(self, repo, mock_workspace_client):
        """Test returns False when file does not exist."""

        async def mock_to_thread(func, *args, **kwargs):
            raise Exception("NOT_FOUND")

        with patch("asyncio.to_thread", side_effect=mock_to_thread):
            result = await repo.file_exists("/Workspace/Apps/my-app/missing.py")

        assert result is False
