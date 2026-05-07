"""Tests for schema name generation."""

import re

from lakebase_accelerator.core.schema_naming import generate_schema_name

SCHEMA_PATTERN = re.compile(r"^project_[a-z0-9_]+_[a-f0-9]{6}$")


class TestGenerateSchemaName:
    """Tests for generate_schema_name function."""

    def test_normal_project_name(self) -> None:
        """Normal project name produces valid schema name."""
        result = generate_schema_name("My Cool Project")
        assert SCHEMA_PATTERN.match(result)
        assert "my_cool_project" in result

    def test_special_characters_sanitized(self) -> None:
        """Special characters are replaced with underscores."""
        result = generate_schema_name("test@project#2024!")
        assert SCHEMA_PATTERN.match(result)
        # No special chars remain
        assert "@" not in result
        assert "#" not in result
        assert "!" not in result

    def test_long_name_truncated(self) -> None:
        """Long names are truncated to fit within 63 chars."""
        long_name = "a" * 200
        result = generate_schema_name(long_name)
        assert len(result) <= 63
        assert SCHEMA_PATTERN.match(result)

    def test_two_calls_produce_different_names(self) -> None:
        """Two calls with the same input produce different names (UUID suffix)."""
        result1 = generate_schema_name("same_project")
        result2 = generate_schema_name("same_project")
        assert result1 != result2

    def test_empty_input_produces_valid_name(self) -> None:
        """Empty-ish input still produces a valid schema name."""
        result = generate_schema_name("")
        assert SCHEMA_PATTERN.match(result)
        assert len(result) <= 63

    def test_whitespace_only_input(self) -> None:
        """Whitespace-only input still produces a valid schema name."""
        result = generate_schema_name("   ")
        assert SCHEMA_PATTERN.match(result)

    def test_output_matches_regex_pattern(self) -> None:
        """Output always matches the expected regex pattern."""
        test_inputs = ["Hello World", "123", "a-b-c", "UPPER", "___", "x" * 100]
        for name in test_inputs:
            result = generate_schema_name(name)
            assert SCHEMA_PATTERN.match(result), f"Failed for input: {name!r}, got: {result}"
            assert len(result) <= 63
