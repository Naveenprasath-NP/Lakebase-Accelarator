"""Tests for YAML prompt loader utility."""

import pytest

from lakebase_accelerator.utils.prompt_loader import _load_prompts, get_prompt, get_system_prompt


class TestPromptLoader:
    """Tests for prompt loader functions."""

    def test_get_prompt_returns_dict_with_version_and_system(self) -> None:
        """Loading a known prompt type returns dict with version and system keys."""
        result = get_prompt("requirement_intake")
        assert "version" in result
        assert "system" in result
        assert result["version"] == "v1"

    def test_get_system_prompt_returns_string(self) -> None:
        """get_system_prompt returns a string."""
        result = get_system_prompt("data_model_inference")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_unknown_prompt_type_raises_key_error(self) -> None:
        """Unknown prompt type raises KeyError."""
        with pytest.raises(KeyError, match="nonexistent_type"):
            get_prompt("nonexistent_type")

    def test_caching_returns_same_object(self) -> None:
        """Caching works - same object returned on repeated calls."""
        # Clear cache to ensure clean state
        _load_prompts.cache_clear()
        result1 = _load_prompts()
        result2 = _load_prompts()
        assert result1 is result2
