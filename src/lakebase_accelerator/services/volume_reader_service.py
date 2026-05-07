"""Volume Reader Service — reads and filters prototype files from Databricks Volume.

Handles zip extraction, file classification, prioritization, and selective reading
to stay within LLM context window limits. Never loads all files into memory at once.
"""

import asyncio
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from lakebase_accelerator.utils.logger import logger

# ─── File Classification ─────────────────────────────────────────────

# Directories to skip entirely
SKIP_DIRECTORIES = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "target",
    ".idea",
    ".vscode",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "egg-info",
}

# Extensions to skip (binary/compiled/irrelevant)
SKIP_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".whl",
    ".tar",
    ".gz",
    ".bz2",
    ".class",
    ".jar",
    ".war",
    ".dll",
    ".so",
    ".dylib",
    ".exe",
    ".bin",
    ".o",
    ".obj",
    ".map",
    ".lock",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".svg",
    ".gif",
    ".bmp",
    ".tiff",
}

# High priority files (schema, DB, models — most relevant for reverse engineering)
HIGH_PRIORITY_PATTERNS = [
    "schema",
    "migration",
    "models.py",
    "model.py",
    "database",
    "db.py",
    ".sql",
    "entities",
    "tables",
    "prisma",
    "typeorm",
    "sequelize",
    "alembic",
    "knex",
    "drizzle",
]

# Medium priority (application code)
MEDIUM_PRIORITY_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".env.sample",
    ".env.example",
}

# Text file extensions we can read
TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".cfg",
    ".ini",
    ".env",
    ".sh",
    ".bat",
    ".dockerfile",
    ".csv",
    ".xml",
    ".graphql",
    ".prisma",
}

# ─── Limits ──────────────────────────────────────────────────────────

MAX_FILES_TO_READ = 25
"""Maximum number of files to read content from."""

MAX_CHARS_PER_FILE = 4000
"""Maximum characters to read per file (truncate beyond this)."""

MAX_TOTAL_CHARS = 80000
"""Maximum total characters across all files (stay within LLM context)."""


class VolumeReaderService:
    """Reads prototype files from Databricks Volume with smart filtering.

    Handles:
    - Zip extraction (in-memory)
    - File classification and prioritization
    - Selective reading with size caps
    - Binary file skipping
    """

    def __init__(self, workspace_client) -> None:
        self._workspace_client = workspace_client

    async def read_prototype_files(self, volume_paths: list[str]) -> dict[str, str]:
        """Read and process prototype files from volume paths.

        Handles zip files by extracting them in-memory.
        Filters, prioritizes, and truncates files to fit LLM context.

        Args:
            volume_paths: List of volume file paths (from upload step).

        Returns:
            Dict of filename → content (filtered, prioritized, truncated).
        """
        logger.info(
            f"Reading prototype files from {len(volume_paths)} volume paths",
            extra={"step": "prototype_ingestion"},
        )

        all_files: dict[str, bytes] = {}

        for path in volume_paths:
            ext = PurePosixPath(path).suffix.lower()

            if ext == ".zip":
                # Extract zip contents
                zip_files = await self._extract_zip(path)
                all_files.update(zip_files)
            elif ext in (".png", ".jpg", ".jpeg", ".pdf"):
                # Binary files — just record their name (can't read as text)
                filename = PurePosixPath(path).name
                all_files[filename] = f"[Binary file: {filename}]".encode("utf-8")
            else:
                # Single text file
                content = await self._read_file(path)
                if content is not None:
                    filename = PurePosixPath(path).name
                    all_files[filename] = content

        logger.info(
            f"Raw files extracted: {len(all_files)} total",
            extra={"step": "prototype_ingestion"},
        )

        # Filter, prioritize, and build text content
        result = self._process_files(all_files)

        logger.info(
            f"Processed files: {len(result)} selected for LLM analysis",
            extra={"step": "prototype_ingestion"},
        )

        return result

    async def _extract_zip(self, volume_path: str) -> dict[str, bytes]:
        """Download and extract a zip file from the volume.

        Returns dict of relative_path → raw bytes for each file in the zip.
        """
        logger.info(f"Extracting zip: {volume_path}", extra={"step": "prototype_ingestion"})

        try:
            # Download zip file content
            response = await asyncio.to_thread(
                self._workspace_client.files.download, volume_path
            )
            zip_bytes = response.contents.read()

            files: dict[str, bytes] = {}
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                for info in zf.infolist():
                    # Skip directories
                    if info.is_dir():
                        continue

                    # Skip files in excluded directories
                    if self._should_skip_path(info.filename):
                        continue

                    # Read file content
                    try:
                        files[info.filename] = zf.read(info.filename)
                    except Exception as e:
                        logger.debug(f"Skipping unreadable zip entry: {info.filename}: {e}")

            logger.info(
                f"Zip extracted: {len(files)} files (after filtering directories)",
                extra={"step": "prototype_ingestion"},
            )
            return files

        except Exception as e:
            logger.warning(f"Failed to extract zip {volume_path}: {e}", extra={"step": "prototype_ingestion"})
            return {}

    async def _read_file(self, volume_path: str) -> bytes | None:
        """Read a single file from the volume."""
        try:
            response = await asyncio.to_thread(
                self._workspace_client.files.download, volume_path
            )
            return response.contents.read()
        except Exception as e:
            logger.warning(f"Failed to read file {volume_path}: {e}", extra={"step": "prototype_ingestion"})
            return None

    def _should_skip_path(self, filepath: str) -> bool:
        """Check if a file path should be skipped based on directory or extension."""
        parts = PurePosixPath(filepath).parts

        # Skip if any directory component is in the skip list
        for part in parts[:-1]:  # Exclude filename itself
            if part.lower() in SKIP_DIRECTORIES or part.endswith(".egg-info"):
                return True

        # Skip by extension
        ext = PurePosixPath(filepath).suffix.lower()
        if ext in SKIP_EXTENSIONS:
            return True

        return False

    def _process_files(self, raw_files: dict[str, bytes]) -> dict[str, str]:
        """Filter, prioritize, and convert raw files to text content.

        Returns a dict of filename → text content, respecting size limits.
        """
        # Classify files into priority buckets
        high_priority: list[tuple[str, bytes]] = []
        medium_priority: list[tuple[str, bytes]] = []
        low_priority: list[tuple[str, bytes]] = []

        for filepath, content in raw_files.items():
            if self._should_skip_path(filepath):
                continue

            ext = PurePosixPath(filepath).suffix.lower()
            name_lower = filepath.lower()

            # Classify priority
            if any(pattern in name_lower for pattern in HIGH_PRIORITY_PATTERNS):
                high_priority.append((filepath, content))
            elif ext in MEDIUM_PRIORITY_EXTENSIONS:
                medium_priority.append((filepath, content))
            elif ext in TEXT_EXTENSIONS:
                low_priority.append((filepath, content))
            # else: skip non-text files

        # Build result respecting limits
        result: dict[str, str] = {}
        total_chars = 0
        files_read = 0

        # Process in priority order
        for filepath, content in high_priority + medium_priority + low_priority:
            if files_read >= MAX_FILES_TO_READ:
                break
            if total_chars >= MAX_TOTAL_CHARS:
                break

            # Try to decode as text
            text = self._decode_content(content)
            if text is None:
                continue

            # Truncate if too long
            if len(text) > MAX_CHARS_PER_FILE:
                text = text[:MAX_CHARS_PER_FILE] + f"\n\n... [truncated, {len(content)} bytes total]"

            # Check total budget
            if total_chars + len(text) > MAX_TOTAL_CHARS:
                remaining = MAX_TOTAL_CHARS - total_chars
                if remaining > 500:  # Only include if we can fit a meaningful chunk
                    text = text[:remaining] + "\n... [truncated to fit context]"
                else:
                    break

            result[filepath] = text
            total_chars += len(text)
            files_read += 1

        return result

    def _decode_content(self, content: bytes) -> str | None:
        """Try to decode bytes as text. Returns None if binary."""
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("latin-1")
            except UnicodeDecodeError:
                return None
