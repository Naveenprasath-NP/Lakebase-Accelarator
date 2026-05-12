"""Response models — standard API envelope for all JSON endpoints.

All responses follow the pattern:
{
    "success": true|false,
    "statusCode": 200,
    "message": "Human-readable message",
    "data": { ... } | null
}
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lakebase_accelerator.models.enums import PipelineStatus, ProjectMode, StepStatus

# ─── Standard Envelope ───────────────────────────────────────────────


class ApiResponse(BaseModel):
    """Standard API response envelope."""

    model_config = ConfigDict(frozen=True)

    success: bool
    statusCode: int  # noqa: N815
    message: str
    data: Any = None


# ─── Health Data ─────────────────────────────────────────────────────


class HealthData(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "healthy"
    version: str = "1.0.0"


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(frozen=True)

    lakebase: str
    model_serving: str


class ReadinessData(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    checks: ReadinessChecks


# ─── Project Data ────────────────────────────────────────────────────


class ProjectSummary(BaseModel):
    """Single project in the list response."""

    model_config = ConfigDict(frozen=True)

    id: str
    project_name: str
    mode: ProjectMode
    status: PipelineStatus
    prompt_preview: str
    app_url: str | None = None
    schema_name: str | None = None
    tables_created: list[str] | None = None
    created_at: str


class ProjectListData(BaseModel):
    """Data payload for project list response."""

    model_config = ConfigDict(frozen=True)

    projects: list[ProjectSummary]
    total: int
    limit: int
    offset: int


class PipelineStepDetail(BaseModel):
    """Step execution detail."""

    model_config = ConfigDict(frozen=True)

    step: str
    status: StepStatus
    duration_ms: int | None = None
    token_usage: int | None = None
    error_message: str | None = None
    error_code: str | None = None


class ProjectDetailData(BaseModel):
    """Data payload for project detail response."""

    model_config = ConfigDict(frozen=True)

    id: str
    project_name: str
    mode: ProjectMode
    status: PipelineStatus
    prompt: str
    app_name: str | None = None
    app_url: str | None = None
    schema_name: str | None = None
    catalog: str | None = None
    tables_created: list[str] = Field(default_factory=list)
    chat_history: list[dict] = Field(default_factory=list)
    pipeline_duration_seconds: float | None = None
    steps: list[PipelineStepDetail] = Field(default_factory=list)
    created_at: str
    updated_at: str


# ─── Helper Functions ────────────────────────────────────────────────


def success_response(message: str, data: Any = None, status_code: int = 200) -> ApiResponse:
    """Create a successful API response."""
    return ApiResponse(success=True, statusCode=status_code, message=message, data=data)


def error_response(message: str, status_code: int, data: Any = None) -> ApiResponse:
    """Create an error API response."""
    return ApiResponse(success=False, statusCode=status_code, message=message, data=data)
