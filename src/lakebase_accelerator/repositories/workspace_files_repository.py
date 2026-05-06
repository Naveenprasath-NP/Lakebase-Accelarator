"""Repository for Databricks Workspace Files API operations."""

import asyncio

from lakebase_accelerator.settings import BACKOFF_BASE_SECONDS, WORKSPACE_APPS_BASE_PATH
from lakebase_accelerator.utils.exceptions import WORKSPACE_WRITE_FAILED, PipelineStepError
from lakebase_accelerator.utils.logger import logger

MAX_WRITE_RETRIES = 3


class WorkspaceFilesRepository:
    """Repository for writing files to Databricks Workspace.

    Uses asyncio.to_thread for sync SDK calls.
    Retries on transient errors with exponential backoff.
    Raises PipelineStepError with WORKSPACE_WRITE_FAILED on failure.
    """

    def __init__(self, workspace_client) -> None:
        self._workspace_client = workspace_client

    async def write_files(self, app_name: str, files: dict[str, str]) -> str:
        """Write all generated files to /Workspace/Apps/{app_name}/.

        Creates directory structure as needed.
        Retries on transient errors with exponential backoff.
        Returns the workspace path.
        """
        base_path = f"{WORKSPACE_APPS_BASE_PATH}/{app_name}"

        logger.info(f"Writing {len(files)} files to workspace: {base_path}")

        # Ensure base directory exists
        await self._mkdirs_with_retry(base_path)

        for relative_path, content in files.items():
            file_path = f"{base_path}/{relative_path}"

            # Ensure parent directory exists
            parent_dir = "/".join(file_path.rsplit("/", 1)[:-1])
            if parent_dir and parent_dir != base_path:
                await self._mkdirs_with_retry(parent_dir)

            # Write file with retry
            await self._write_file_with_retry(file_path, content)

        logger.info(f"Successfully wrote {len(files)} files to {base_path}")
        return base_path

    async def file_exists(self, path: str) -> bool:
        """Check if a file exists at the given workspace path."""
        try:
            await asyncio.to_thread(self._workspace_client.workspace.get_status, path)
            return True
        except Exception:
            return False

    async def _mkdirs_with_retry(self, path: str) -> None:
        """Create directory structure with retry on transient errors."""
        for attempt in range(MAX_WRITE_RETRIES):
            try:
                await asyncio.to_thread(self._workspace_client.workspace.mkdirs, path)
                return
            except Exception as e:
                if attempt < MAX_WRITE_RETRIES - 1:
                    wait_time = BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(f"Retrying mkdirs for {path} after {wait_time}s (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise PipelineStepError(
                        step_name="workspace_write",
                        message=f"Failed to create directory '{path}': {e}",
                        error_code=WORKSPACE_WRITE_FAILED,
                    ) from e

    async def _write_file_with_retry(self, path: str, content: str) -> None:
        """Write a single file with retry on transient errors."""
        for attempt in range(MAX_WRITE_RETRIES):
            try:
                await asyncio.to_thread(
                    self._workspace_client.workspace.import_,
                    path,
                    content=content.encode("utf-8"),
                    format="AUTO",
                    overwrite=True,
                )
                logger.debug(f"Wrote file: {path}")
                return
            except Exception as e:
                if attempt < MAX_WRITE_RETRIES - 1:
                    wait_time = BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(f"Retrying write for {path} after {wait_time}s (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise PipelineStepError(
                        step_name="workspace_write",
                        message=f"Failed to write file '{path}': {e}",
                        error_code=WORKSPACE_WRITE_FAILED,
                    ) from e
