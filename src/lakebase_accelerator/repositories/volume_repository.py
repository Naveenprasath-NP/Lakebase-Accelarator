"""Repository for Databricks Unity Catalog Volume file operations.

Uploads binary files to a UC Volume using the Databricks SDK Files API.
Volume path: /Volumes/{catalog}/{schema}/{volume}/{project_id}/{filename}
"""

import asyncio
from io import BytesIO

from lakebase_accelerator.settings import BACKOFF_BASE_SECONDS
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import VOLUME_UPLOAD_FAILED
from lakebase_accelerator.utils.logger import logger

MAX_UPLOAD_RETRIES = 3


class VolumeRepository:
    """Repository for uploading files to Databricks Unity Catalog Volumes.

    Uses the Databricks SDK workspace_client.files API.
    Retries on transient errors with exponential backoff.
    """

    def __init__(self, workspace_client, catalog: str, schema: str, volume: str) -> None:
        self._workspace_client = workspace_client
        self._catalog = catalog
        self._schema = schema
        self._volume = volume

    @property
    def volume_base_path(self) -> str:
        """Return the base volume path in DBFS format."""
        return f"/Volumes/{self._catalog}/{self._schema}/{self._volume}"

    async def upload_file(self, project_id: str, filename: str, content: bytes) -> str:
        """Upload a single file to the volume under a project-specific directory.

        Args:
            project_id: Unique project identifier (used as subdirectory).
            filename: Original filename to preserve.
            content: Raw binary content of the file.

        Returns:
            Full volume path where the file was stored.

        Raises:
            PipelineStepError: If upload fails after retries.
        """
        file_path = f"{self.volume_base_path}/{project_id}/{filename}"

        logger.info(f"Uploading file to volume: {file_path}", extra={"step": "volume_upload"})

        for attempt in range(MAX_UPLOAD_RETRIES):
            try:
                await asyncio.to_thread(
                    self._workspace_client.files.upload,
                    file_path,
                    BytesIO(content),
                    overwrite=True,
                )
                logger.info(f"Successfully uploaded: {file_path}", extra={"step": "volume_upload"})
                return file_path

            except Exception as e:
                if attempt < MAX_UPLOAD_RETRIES - 1:
                    wait_time = BACKOFF_BASE_SECONDS * (2**attempt)
                    logger.warning(
                        f"Retrying volume upload for {file_path} after {wait_time}s "
                        f"(attempt {attempt + 1}): {e}",
                        extra={"step": "volume_upload"},
                    )
                    await asyncio.sleep(wait_time)
                else:
                    raise PipelineStepError(
                        step_name="volume_upload",
                        message=f"Failed to upload file '{filename}' to volume: {e}",
                        error_code=VOLUME_UPLOAD_FAILED,
                    ) from e

        # Should not reach here, but satisfy type checker
        raise PipelineStepError(
            step_name="volume_upload",
            message=f"Failed to upload file '{filename}' after {MAX_UPLOAD_RETRIES} attempts",
            error_code=VOLUME_UPLOAD_FAILED,
        )

    async def upload_files(self, project_id: str, files: dict[str, bytes]) -> list[str]:
        """Upload multiple files to the volume.

        Args:
            project_id: Unique project identifier (used as subdirectory).
            files: Dict of filename → binary content.

        Returns:
            List of full volume paths where files were stored.
        """
        logger.info(
            f"Uploading {len(files)} files to volume for project: {project_id}",
            extra={"step": "volume_upload"},
        )

        volume_paths = []
        for filename, content in files.items():
            path = await self.upload_file(project_id, filename, content)
            volume_paths.append(path)

        logger.info(
            f"All {len(volume_paths)} files uploaded successfully",
            extra={"step": "volume_upload", "project_id": project_id},
        )
        return volume_paths

    async def file_exists(self, path: str) -> bool:
        """Check if a file exists at the given volume path."""
        try:
            await asyncio.to_thread(self._workspace_client.files.get_status, path)
            return True
        except Exception:
            return False
