"""Brownfield agent tools — iterative exploration tools for the ReAct agent.

These tools allow the brownfield exploration agent to navigate and analyze
extracted prototype code from a volume cache. The cache is populated by the
exploration node after zip extraction and maps relative file paths to their
raw bytes content.

Tools:
- list_volume_directory: List files/dirs at a path
- read_volume_file: Read full file content (max 50KB)
- read_volume_file_section: Read specific line range
- search_in_files: Regex search across files matching glob
- get_file_metadata: Get file size and extension without reading content
"""

import fnmatch
import json
import os
import re

from langchain_core.tools import tool

from lakebase_accelerator.utils.logger import logger

# ─── Constants ────────────────────────────────────────────────────────

MAX_FILE_SIZE_BYTES = 50 * 1024
"""Maximum file size (50KB) for read_volume_file."""

MAX_SEARCH_RESULTS = 50
"""Maximum number of search results to return."""

MAX_CONTEXT_LINES = 2
"""Number of context lines around each search match."""

# ─── Module-Level Volume Cache ────────────────────────────────────────

_volume_cache: dict[str, bytes] = {}
"""Module-level cache mapping relative file paths to raw bytes content.

Populated by the exploration node after extracting uploaded zip files.
All tools operate on this cache rather than reading from disk or volume directly.
"""


def set_volume_cache(cache: dict[str, bytes]) -> None:
    """Set the module-level volume cache with extracted file contents.

    Called by the brownfield exploration node after extracting uploaded files.

    Args:
        cache: Dict mapping relative file paths to raw bytes content.
    """
    global _volume_cache
    _volume_cache = cache
    logger.info(f"Volume cache set with {len(cache)} files")


def get_volume_cache() -> dict[str, bytes]:
    """Get the current volume cache (primarily for testing)."""
    return _volume_cache


def _normalize_path(path: str) -> str:
    """Normalize a path by removing leading/trailing slashes and collapsing separators."""
    return os.path.normpath(path).strip(os.sep).replace("\\", "/")


# ═══════════════════════════════════════════════════════════════════════
# TOOL: List Volume Directory
# ═══════════════════════════════════════════════════════════════════════


@tool
def list_volume_directory(path: str) -> str:
    """List files and subdirectories at a given path in the extracted volume.

    Returns a structured list of entries with name, type (file/directory),
    size in bytes, and file extension for each item at the specified path.

    Args:
        path: Relative directory path to list. Use "" or "." for root.

    Returns:
        JSON string with list of entries, each having name, type, size, and extension.
    """
    normalized = _normalize_path(path) if path and path not in ("", ".") else ""

    entries: dict[str, dict] = {}

    for file_path, content in _volume_cache.items():
        # Normalize the cached path for comparison
        norm_file = file_path.replace("\\", "/")

        # Check if this file is under the requested directory
        if normalized:
            if not norm_file.startswith(normalized + "/"):
                continue
            # Get the relative part after the directory prefix
            relative = norm_file[len(normalized) + 1:]
        else:
            relative = norm_file

        # Split into parts to find immediate children
        parts = relative.split("/")

        if len(parts) == 1:
            # Direct child file
            name = parts[0]
            ext = os.path.splitext(name)[1]
            entries[name] = {
                "name": name,
                "type": "file",
                "size": len(content),
                "extension": ext,
            }
        else:
            # This file is in a subdirectory — record the directory
            dir_name = parts[0]
            if dir_name not in entries:
                entries[dir_name] = {
                    "name": dir_name,
                    "type": "directory",
                    "size": 0,
                    "extension": "",
                }

    result = sorted(entries.values(), key=lambda e: (e["type"] == "file", e["name"]))
    return json.dumps({"path": path, "entries": result, "count": len(result)})


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Read Volume File
# ═══════════════════════════════════════════════════════════════════════


@tool
def read_volume_file(path: str) -> str:
    """Read the full content of a specific file from the extracted volume.

    Returns the file content as text. Returns an error if the file is binary
    (cannot be decoded as UTF-8) or exceeds the 50KB size limit.

    Args:
        path: Relative file path within the extracted volume.

    Returns:
        JSON string with file content or error message.
    """
    normalized = _normalize_path(path)

    # Look up the file in cache (try both normalized and original)
    content = _volume_cache.get(normalized) or _volume_cache.get(path)

    if content is None:
        # Try matching with forward slashes
        for cached_path, cached_content in _volume_cache.items():
            if _normalize_path(cached_path) == normalized:
                content = cached_content
                break

    if content is None:
        return json.dumps({"error": f"File not found: {path}"})

    # Check size limit
    if len(content) > MAX_FILE_SIZE_BYTES:
        return json.dumps({
            "error": f"File too large: {len(content)} bytes (max {MAX_FILE_SIZE_BYTES} bytes). "
            "Use read_volume_file_section to read specific line ranges.",
            "size": len(content),
        })

    # Try to decode as UTF-8
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps({
            "error": f"Binary file cannot be read as text: {path}",
            "size": len(content),
        })

    return json.dumps({"path": path, "content": text, "size": len(content)})


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Read Volume File Section
# ═══════════════════════════════════════════════════════════════════════


@tool
def read_volume_file_section(path: str, start_line: int, end_line: int) -> str:
    """Read a specific line range from a file in the extracted volume.

    Useful for reading specific sections of large files (e.g., class definitions,
    function bodies) without loading the entire file.

    Args:
        path: Relative file path within the extracted volume.
        start_line: Starting line number (1-indexed, inclusive).
        end_line: Ending line number (1-indexed, inclusive).

    Returns:
        JSON string with the requested lines or error message.
    """
    normalized = _normalize_path(path)

    # Look up the file in cache
    content = _volume_cache.get(normalized) or _volume_cache.get(path)

    if content is None:
        for cached_path, cached_content in _volume_cache.items():
            if _normalize_path(cached_path) == normalized:
                content = cached_content
                break

    if content is None:
        return json.dumps({"error": f"File not found: {path}"})

    # Try to decode as UTF-8
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps({"error": f"Binary file cannot be read as text: {path}"})

    lines = text.splitlines()
    total_lines = len(lines)

    # Validate line range
    if start_line < 1:
        start_line = 1
    if end_line > total_lines:
        end_line = total_lines
    if start_line > end_line:
        return json.dumps({
            "error": f"Invalid line range: start_line ({start_line}) > end_line ({end_line})",
            "total_lines": total_lines,
        })

    # Extract the requested section (convert to 0-indexed)
    selected_lines = lines[start_line - 1:end_line]
    section_text = "\n".join(selected_lines)

    return json.dumps({
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "content": section_text,
    })


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Search In Files
# ═══════════════════════════════════════════════════════════════════════


@tool
def search_in_files(pattern: str, file_glob: str) -> str:
    """Search for a regex pattern across files matching a glob pattern.

    Searches all text files in the volume that match the glob pattern and
    returns matching file paths with line numbers and surrounding context.

    Args:
        pattern: Regex pattern to search for (Python re syntax).
        file_glob: Glob pattern to filter files (e.g., "*.py", "**/*.ts", "src/**/*.js").

    Returns:
        JSON string with matching results including file path, line number, and context.
    """
    try:
        compiled_pattern = re.compile(pattern)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex pattern: {e}"})

    matches: list[dict] = []

    for file_path, content in _volume_cache.items():
        # Check if file matches the glob pattern
        norm_path = file_path.replace("\\", "/")
        if not fnmatch.fnmatch(norm_path, file_glob):
            continue

        # Try to decode as text
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue

        # Search line by line
        lines = text.splitlines()
        for line_num, line in enumerate(lines, start=1):
            if compiled_pattern.search(line):
                # Build context (surrounding lines)
                context_start = max(0, line_num - 1 - MAX_CONTEXT_LINES)
                context_end = min(len(lines), line_num + MAX_CONTEXT_LINES)
                context_lines = lines[context_start:context_end]

                matches.append({
                    "file": norm_path,
                    "line": line_num,
                    "match": line.strip(),
                    "context": "\n".join(context_lines),
                })

                if len(matches) >= MAX_SEARCH_RESULTS:
                    return json.dumps({
                        "pattern": pattern,
                        "file_glob": file_glob,
                        "matches": matches,
                        "total_matches": len(matches),
                        "truncated": True,
                    })

    return json.dumps({
        "pattern": pattern,
        "file_glob": file_glob,
        "matches": matches,
        "total_matches": len(matches),
        "truncated": False,
    })


# ═══════════════════════════════════════════════════════════════════════
# TOOL: Get File Metadata
# ═══════════════════════════════════════════════════════════════════════


@tool
def get_file_metadata(path: str) -> str:
    """Get file size and extension without reading the full content.

    Useful for checking file size before deciding whether to read it fully
    or use read_volume_file_section for large files.

    Args:
        path: Relative file path within the extracted volume.

    Returns:
        JSON string with file size and extension, or error if not found.
    """
    normalized = _normalize_path(path)

    # Look up the file in cache
    content = _volume_cache.get(normalized) or _volume_cache.get(path)

    if content is None:
        for cached_path, cached_content in _volume_cache.items():
            if _normalize_path(cached_path) == normalized:
                content = cached_content
                break

    if content is None:
        return json.dumps({"error": f"File not found: {path}"})

    ext = os.path.splitext(normalized)[1]
    size = len(content)

    # Determine if file is likely text or binary
    is_text = True
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False

    return json.dumps({
        "path": path,
        "size": size,
        "extension": ext,
        "is_text": is_text,
    })


# ═══════════════════════════════════════════════════════════════════════
# ALL BROWNFIELD TOOLS LIST
# ═══════════════════════════════════════════════════════════════════════

BROWNFIELD_TOOLS = [
    list_volume_directory,
    read_volume_file,
    read_volume_file_section,
    search_in_files,
    get_file_metadata,
]
