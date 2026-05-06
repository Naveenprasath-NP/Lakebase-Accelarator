"""Tests for request model validation."""

import pytest
from pydantic import ValidationError

from lakebase_accelerator.models.requests import GreenFieldRequest


class TestGreenFieldRequest:
    """Tests for GreenFieldRequest validation."""

    def test_valid_prompt(self):
        """Valid prompt is accepted."""
        req = GreenFieldRequest(prompt="Build a customer service app")
        assert req.prompt == "Build a customer service app"

    def test_prompt_stripped(self):
        """Prompt whitespace is stripped."""
        req = GreenFieldRequest(prompt="  Build an app  ")
        assert req.prompt == "Build an app"

    def test_empty_prompt_rejected(self):
        """Empty prompt raises ValidationError."""
        with pytest.raises(ValidationError):
            GreenFieldRequest(prompt="")

    def test_whitespace_prompt_rejected(self):
        """Whitespace-only prompt raises ValidationError."""
        with pytest.raises(ValidationError):
            GreenFieldRequest(prompt="   ")

    def test_oversized_prompt_rejected(self):
        """Prompt exceeding 10000 chars raises ValidationError."""
        with pytest.raises(ValidationError):
            GreenFieldRequest(prompt="x" * 10_001)

    def test_valid_project_name(self):
        """Valid kebab-case project name is accepted."""
        req = GreenFieldRequest(prompt="Build an app", project_name="my-app")
        assert req.project_name == "my-app"

    def test_invalid_project_name_uppercase(self):
        """Uppercase project name raises ValidationError."""
        with pytest.raises(ValidationError):
            GreenFieldRequest(prompt="Build an app", project_name="MyApp")

    def test_invalid_project_name_spaces(self):
        """Project name with spaces raises ValidationError."""
        with pytest.raises(ValidationError):
            GreenFieldRequest(prompt="Build an app", project_name="my app")

    def test_none_project_name_allowed(self):
        """None project name is allowed (auto-generated)."""
        req = GreenFieldRequest(prompt="Build an app", project_name=None)
        assert req.project_name is None

    def test_frozen_model(self):
        """Frozen model prevents attribute mutation."""
        req = GreenFieldRequest(prompt="Build an app")
        with pytest.raises(ValidationError):
            req.prompt = "Changed"
