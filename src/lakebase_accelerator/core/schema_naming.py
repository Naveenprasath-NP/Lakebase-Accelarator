"""Schema name generation for PostgreSQL identifiers."""

import re
from uuid import uuid4

from lakebase_accelerator.settings import MAX_SCHEMA_NAME_LENGTH, SCHEMA_PREFIX, SCHEMA_SUFFIX_LENGTH


def generate_schema_name(project_name: str) -> str:
    """Generate a valid PostgreSQL schema name from a project name.

    Rules:
        - Prefix: "project_"
        - Sanitize: lowercase, replace non-alphanumeric with underscore,
          collapse multiple underscores, strip leading/trailing underscores
        - Suffix: 6-char hex from uuid4
        - Max total length: 63 chars (PostgreSQL identifier limit)
        - Output matches: project_[a-z0-9_]+_[a-f0-9]{6}

    Args:
        project_name: Human-readable project name.

    Returns:
        A unique, valid PostgreSQL identifier (max 63 chars).
    """
    # Sanitize: lowercase, replace non-alphanumeric with underscore
    sanitized = project_name.lower()
    sanitized = re.sub(r"[^a-z0-9]", "_", sanitized)

    # Collapse multiple underscores and strip leading/trailing
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")

    # If sanitized is empty after stripping, use a fallback
    if not sanitized:
        sanitized = "unnamed"

    # Generate suffix
    suffix = uuid4().hex[:SCHEMA_SUFFIX_LENGTH]

    # Calculate max allowed length for the sanitized portion
    # Format: {SCHEMA_PREFIX}{sanitized}_{suffix}
    # overhead = len(prefix) + 1 (underscore before suffix) + suffix length
    overhead = len(SCHEMA_PREFIX) + 1 + SCHEMA_SUFFIX_LENGTH
    max_name_length = MAX_SCHEMA_NAME_LENGTH - overhead

    # Truncate sanitized name if needed
    if len(sanitized) > max_name_length:
        sanitized = sanitized[:max_name_length].rstrip("_")

    return f"{SCHEMA_PREFIX}{sanitized}_{suffix}"
