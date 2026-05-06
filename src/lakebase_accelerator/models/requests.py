"""Request models for API endpoints."""

from pydantic import BaseModel, ConfigDict, field_validator

from lakebase_accelerator.settings import PROMPT_MAX_LENGTH, PROMPT_MIN_LENGTH


class GreenFieldRequest(BaseModel):
    """Request body for POST /api/v1/projects/greenfield."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    """Natural-language business prompt describing the application to build."""

    project_name: str | None = None
    """Optional project name in kebab-case. Auto-generated from prompt if not provided."""

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < PROMPT_MIN_LENGTH:
            msg = "Prompt must not be empty"
            raise ValueError(msg)
        if len(stripped) > PROMPT_MAX_LENGTH:
            msg = f"Prompt must not exceed {PROMPT_MAX_LENGTH} characters"
            raise ValueError(msg)
        return stripped

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re

        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            msg = "Project name must be kebab-case (lowercase alphanumeric with hyphens)"
            raise ValueError(msg)
        if len(v) > 100:
            msg = "Project name must not exceed 100 characters"
            raise ValueError(msg)
        return v


class BrownFieldRequest(BaseModel):
    """Request body for POST /api/v1/projects/brownfield (JSON fields from multipart)."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    """Description of the existing prototype or context for the migration."""

    project_name: str | None = None
    """Optional project name in kebab-case. Auto-generated if not provided."""

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < PROMPT_MIN_LENGTH:
            msg = "Prompt must not be empty"
            raise ValueError(msg)
        if len(stripped) > PROMPT_MAX_LENGTH:
            msg = f"Prompt must not exceed {PROMPT_MAX_LENGTH} characters"
            raise ValueError(msg)
        return stripped

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re

        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            msg = "Project name must be kebab-case (lowercase alphanumeric with hyphens)"
            raise ValueError(msg)
        if len(v) > 100:
            msg = "Project name must not exceed 100 characters"
            raise ValueError(msg)
        return v
