"""Pipeline event models for SSE streaming."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from lakebase_accelerator.models.enums import PipelineStep, StepStatus


class PipelineEvent(BaseModel):
    """Server-Sent Event emitted during pipeline execution."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    """Event type: step_started, step_completed, step_failed, pipeline_complete."""

    step: PipelineStep | None = None
    """Pipeline step name. None for pipeline_complete events."""

    status: StepStatus | None = None
    """Status of the step or pipeline."""

    message: str = ""
    """Human-readable progress message."""

    data: dict = Field(default_factory=dict)
    """Step-specific payload (app_url, schema_name, error_code, etc.)."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PipelineResult(BaseModel):
    """Final result of a completed pipeline."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    project_name: str
    mode: str
    schema_name: str
    app_name: str
    app_url: str
    catalog: str
    tables_created: list[str]
    seed_data_row_counts: dict[str, int] = Field(default_factory=dict)
    service_principal_id: str
    pipeline_duration_seconds: float
    total_token_usage: int
    steps_completed: list[str] = Field(default_factory=list)
