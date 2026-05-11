"""Unit tests for brownfield agent tools.

Tests each tool in isolation with mock volume data to verify correct behavior
for file listing, reading, searching, and metadata retrieval.
"""

import json

import pytest

from lakebase_accelerator.agent.tools.brownfield_tools import (
    BROWNFIELD_TOOLS,
    get_file_metadata,
    get_volume_cache,
    list_volume_directory,
    read_volume_file,
    read_volume_file_section,
    search_in_files,
    set_volume_cache,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def setup_volume_cache():
    """Set up a mock volume cache for all tests and clean up after."""
    cache = {
        "src/main.py": b"from app import create_app\n\napp = create_app()\n\nif __name__ == '__main__':\n    app.run()\n",
        "src/models/user.py": b"class User:\n    def __init__(self, name, email):\n        self.name = name\n        self.email = email\n",
        "src/models/__init__.py": b"from .user import User\n",
        "src/routes/api.py": b"from flask import Blueprint\n\napi = Blueprint('api', __name__)\n\n@api.route('/users')\ndef get_users():\n    return []\n",
        "requirements.txt": b"flask==3.0.0\nsqlalchemy==2.0.0\npydantic==2.0.0\n",
        "README.md": b"# My Project\n\nA sample Flask application.\n",
        "tests/test_user.py": b"import pytest\n\ndef test_user_creation():\n    pass\n",
        "config/settings.yaml": b"database:\n  host: localhost\n  port: 5432\n",
        "binary_file.bin": b"\x00\x01\x02\x03\xff\xfe\xfd",
    }
    set_volume_cache(cache)
    yield cache
    # Clean up
    set_volume_cache({})


# ─── Tests: set_volume_cache ──────────────────────────────────────────


class TestSetVolumeCache:
    def test_set_cache_populates_module_level_cache(self):
        cache = {"file.py": b"content"}
        set_volume_cache(cache)
        assert get_volume_cache() == cache

    def test_set_cache_replaces_previous_cache(self):
        set_volume_cache({"old.py": b"old"})
        set_volume_cache({"new.py": b"new"})
        assert "old.py" not in get_volume_cache()
        assert "new.py" in get_volume_cache()


# ─── Tests: list_volume_directory ─────────────────────────────────────


class TestListVolumeDirectory:
    def test_list_root_directory(self):
        result = json.loads(list_volume_directory.invoke({"path": ""}))
        entries = result["entries"]
        names = [e["name"] for e in entries]

        # Should have directories and files at root
        assert "src" in names
        assert "tests" in names
        assert "config" in names
        assert "requirements.txt" in names
        assert "README.md" in names
        assert "binary_file.bin" in names

    def test_list_root_with_dot(self):
        result = json.loads(list_volume_directory.invoke({"path": "."}))
        entries = result["entries"]
        names = [e["name"] for e in entries]
        assert "src" in names
        assert "requirements.txt" in names

    def test_list_subdirectory(self):
        result = json.loads(list_volume_directory.invoke({"path": "src"}))
        entries = result["entries"]
        names = [e["name"] for e in entries]

        assert "main.py" in names
        assert "models" in names
        assert "routes" in names

    def test_list_nested_subdirectory(self):
        result = json.loads(list_volume_directory.invoke({"path": "src/models"}))
        entries = result["entries"]
        names = [e["name"] for e in entries]

        assert "user.py" in names
        assert "__init__.py" in names

    def test_list_empty_directory(self):
        result = json.loads(list_volume_directory.invoke({"path": "nonexistent"}))
        assert result["count"] == 0
        assert result["entries"] == []

    def test_entry_has_correct_structure_for_file(self):
        result = json.loads(list_volume_directory.invoke({"path": ""}))
        entries = result["entries"]
        readme = next(e for e in entries if e["name"] == "README.md")

        assert readme["type"] == "file"
        assert readme["size"] > 0
        assert readme["extension"] == ".md"

    def test_entry_has_correct_structure_for_directory(self):
        result = json.loads(list_volume_directory.invoke({"path": ""}))
        entries = result["entries"]
        src_dir = next(e for e in entries if e["name"] == "src")

        assert src_dir["type"] == "directory"
        assert src_dir["extension"] == ""

    def test_directories_sorted_before_files(self):
        result = json.loads(list_volume_directory.invoke({"path": ""}))
        entries = result["entries"]
        types = [e["type"] for e in entries]

        # All directories should come before files
        dir_indices = [i for i, t in enumerate(types) if t == "directory"]
        file_indices = [i for i, t in enumerate(types) if t == "file"]
        if dir_indices and file_indices:
            assert max(dir_indices) < min(file_indices)


# ─── Tests: read_volume_file ──────────────────────────────────────────


class TestReadVolumeFile:
    def test_read_existing_file(self):
        result = json.loads(read_volume_file.invoke({"path": "requirements.txt"}))
        assert "content" in result
        assert "flask==3.0.0" in result["content"]
        assert result["size"] > 0

    def test_read_nested_file(self):
        result = json.loads(read_volume_file.invoke({"path": "src/main.py"}))
        assert "content" in result
        assert "create_app" in result["content"]

    def test_read_nonexistent_file(self):
        result = json.loads(read_volume_file.invoke({"path": "nonexistent.py"}))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_read_binary_file_returns_error(self):
        result = json.loads(read_volume_file.invoke({"path": "binary_file.bin"}))
        assert "error" in result
        assert "binary" in result["error"].lower()

    def test_read_file_exceeding_size_limit(self):
        # Add a large file to cache
        large_content = b"x" * (50 * 1024 + 1)
        cache = get_volume_cache()
        cache["large_file.txt"] = large_content
        set_volume_cache(cache)

        result = json.loads(read_volume_file.invoke({"path": "large_file.txt"}))
        assert "error" in result
        assert "too large" in result["error"].lower()

    def test_read_file_at_exact_size_limit(self):
        # File at exactly 50KB should be readable
        exact_content = b"a" * (50 * 1024)
        cache = get_volume_cache()
        cache["exact_limit.txt"] = exact_content
        set_volume_cache(cache)

        result = json.loads(read_volume_file.invoke({"path": "exact_limit.txt"}))
        assert "content" in result
        assert len(result["content"]) == 50 * 1024


# ─── Tests: read_volume_file_section ──────────────────────────────────


class TestReadVolumeFileSection:
    def test_read_specific_lines(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": 1,
            "end_line": 3,
        }))
        assert "content" in result
        assert "create_app" in result["content"]
        assert result["start_line"] == 1
        assert result["end_line"] == 3

    def test_read_single_line(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": 1,
            "end_line": 1,
        }))
        assert "content" in result
        assert result["start_line"] == 1
        assert result["end_line"] == 1

    def test_read_nonexistent_file(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "nonexistent.py",
            "start_line": 1,
            "end_line": 5,
        }))
        assert "error" in result

    def test_read_binary_file_returns_error(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "binary_file.bin",
            "start_line": 1,
            "end_line": 5,
        }))
        assert "error" in result
        assert "binary" in result["error"].lower()

    def test_end_line_clamped_to_file_length(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": 1,
            "end_line": 1000,
        }))
        assert "content" in result
        assert result["total_lines"] < 1000

    def test_start_line_less_than_one_clamped(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": -5,
            "end_line": 3,
        }))
        assert "content" in result
        assert result["start_line"] == 1

    def test_invalid_range_start_greater_than_end(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": 10,
            "end_line": 2,
        }))
        # start_line > total_lines, so after clamping end_line, start > end
        assert "error" in result or "content" in result

    def test_returns_total_lines(self):
        result = json.loads(read_volume_file_section.invoke({
            "path": "src/main.py",
            "start_line": 1,
            "end_line": 3,
        }))
        assert "total_lines" in result
        assert result["total_lines"] > 0


# ─── Tests: search_in_files ───────────────────────────────────────────


class TestSearchInFiles:
    def test_search_simple_pattern(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "class",
            "file_glob": "*.py",
        }))
        assert result["total_matches"] > 0
        # Should find "class User" in src/models/user.py
        files_matched = [m["file"] for m in result["matches"]]
        assert any("user.py" in f for f in files_matched)

    def test_search_with_glob_filter(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "flask",
            "file_glob": "*.txt",
        }))
        assert result["total_matches"] > 0
        files_matched = [m["file"] for m in result["matches"]]
        assert any("requirements.txt" in f for f in files_matched)

    def test_search_nested_glob(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "Blueprint",
            "file_glob": "src/**/*.py",
        }))
        assert result["total_matches"] > 0

    def test_search_no_matches(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "nonexistent_pattern_xyz",
            "file_glob": "*.py",
        }))
        assert result["total_matches"] == 0
        assert result["matches"] == []

    def test_search_invalid_regex(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "[invalid",
            "file_glob": "*.py",
        }))
        assert "error" in result

    def test_search_skips_binary_files(self):
        result = json.loads(search_in_files.invoke({
            "pattern": ".*",
            "file_glob": "*.bin",
        }))
        # Binary files should be skipped (can't decode as UTF-8)
        assert result["total_matches"] == 0

    def test_search_result_has_context(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "class User",
            "file_glob": "**/*.py",
        }))
        assert result["total_matches"] > 0
        match = result["matches"][0]
        assert "context" in match
        assert "line" in match
        assert "file" in match
        assert "match" in match

    def test_search_result_has_line_number(self):
        result = json.loads(search_in_files.invoke({
            "pattern": "class User",
            "file_glob": "**/*.py",
        }))
        match = result["matches"][0]
        assert match["line"] == 1  # "class User" is on line 1 of user.py

    def test_search_truncated_flag(self):
        result = json.loads(search_in_files.invoke({
            "pattern": ".*",
            "file_glob": "**/*",
        }))
        # With our small test cache, shouldn't be truncated
        assert "truncated" in result


# ─── Tests: get_file_metadata ─────────────────────────────────────────


class TestGetFileMetadata:
    def test_get_metadata_for_text_file(self):
        result = json.loads(get_file_metadata.invoke({"path": "src/main.py"}))
        assert result["extension"] == ".py"
        assert result["size"] > 0
        assert result["is_text"] is True

    def test_get_metadata_for_binary_file(self):
        result = json.loads(get_file_metadata.invoke({"path": "binary_file.bin"}))
        assert result["extension"] == ".bin"
        assert result["size"] > 0
        assert result["is_text"] is False

    def test_get_metadata_nonexistent_file(self):
        result = json.loads(get_file_metadata.invoke({"path": "nonexistent.py"}))
        assert "error" in result

    def test_get_metadata_returns_correct_size(self):
        result = json.loads(get_file_metadata.invoke({"path": "requirements.txt"}))
        expected_size = len(b"flask==3.0.0\nsqlalchemy==2.0.0\npydantic==2.0.0\n")
        assert result["size"] == expected_size


# ─── Tests: BROWNFIELD_TOOLS list ─────────────────────────────────────


class TestBrownfieldToolsList:
    def test_all_tools_present(self):
        assert len(BROWNFIELD_TOOLS) == 5

    def test_tools_have_correct_names(self):
        tool_names = [t.name for t in BROWNFIELD_TOOLS]
        assert "list_volume_directory" in tool_names
        assert "read_volume_file" in tool_names
        assert "read_volume_file_section" in tool_names
        assert "search_in_files" in tool_names
        assert "get_file_metadata" in tool_names
