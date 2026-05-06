"""Volume Upload Service — validates and uploads brownfield files to Databricks Volume.

Handles file validation (size, type, count) and orchestrates the upload
to the Unity Catalog Volume via VolumeRepository.
"""

import uuid
from pathlib import PurePosixPath

from fastapi import UploadFile

from lakebase_accelerator.repositories.volume_repository import VolumeRepository
from lakebase_accelerator.settings import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_SIZE_BYTES,
)
from lakebase_accelerator.utils.logger import logger

# Per-file size limit (5MB as per OpenAPI spec)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


class FileValidationError(Exception):
    """Raised when uploaded files fail validation."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class VolumeUploadService:
    """Validates uploaded files and stores them in a Databricks Volume."""

    def __init__(self, volume_repo: VolumeRepository) -> None:
        self._volume_repo = volume_repo

    async def validate_and_upload(self, files: list[UploadFile]) -> list[str]:
        """Validate uploaded files and upload them to the volume.

        Generates a unique project_id subdirectory for each upload batch.

        Args:
            files: List of FastAPI UploadFile objects from multipart form.

        Returns:
            List of volume paths where files were stored.

        Raises:
            FileValidationError: If files fail validation (count, size, type).
        """
        # Validate file count
        if not files:
            raise FileValidationError("At least one file is required for brownfield pipelines")

        if len(files) > MAX_UPLOAD_FILES:
            raise FileValidationError(f"Maximum {MAX_UPLOAD_FILES} files allowed per upload")

        # Generate unique project subdirectory
        project_id = uuid.uuid4().hex[:12]

        logger.info(
            f"Processing {len(files)} files for volume upload",
            extra={"step": "volume_upload", "project_id": project_id},
        )

        # Validate and read all files
        file_contents: dict[str, bytes] = {}
        total_size = 0

        for upload_file in files:
            filename = upload_file.filename or "unnamed_file"

            # Validate file extension
            ext = PurePosixPath(filename).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
                raise FileValidationError(
                    f"Unsupported file type '{ext}'. Allowed: {allowed}"
                )

            # Read file content
            content = await upload_file.read()

            # Validate individual file size
            if len(content) > MAX_FILE_SIZE_BYTES:
                raise FileValidationError(
                    f"File '{filename}' exceeds maximum size of 5MB"
                )

            total_size += len(content)

            # Validate total upload size
            if total_size > MAX_UPLOAD_SIZE_BYTES:
                raise FileValidationError(
                    f"Total upload size exceeds maximum of {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB"
                )

            file_contents[filename] = content

        # Upload all validated files to volume
        volume_paths = await self._volume_repo.upload_files(project_id, file_contents)

        logger.info(
            f"Volume upload complete: {len(volume_paths)} files stored",
            extra={"step": "volume_upload", "project_id": project_id},
        )

        return volume_paths
