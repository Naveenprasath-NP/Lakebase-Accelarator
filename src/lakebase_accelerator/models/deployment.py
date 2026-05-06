"""Deployment-related models for Databricks Apps API interactions."""

from pydantic import BaseModel, ConfigDict, Field


class AppResource(BaseModel):
    """A Databricks App resource attachment."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Resource name (e.g., 'lakebase', 'model-serving')."""

    resource_type: str
    """Resource type identifier."""

    config: dict = Field(default_factory=dict)
    """Resource-specific configuration."""


class AppInfo(BaseModel):
    """App metadata from Databricks Apps API."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str | None = None
    service_principal_id: str | None = None
    status: str


class DeploymentInfo(BaseModel):
    """Deployment metadata returned by Databricks Apps API."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    app_name: str
    status: str
    source_path: str


class DeploymentStatus(BaseModel):
    """Deployment polling result."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    status: str
    """PENDING, IN_PROGRESS, READY, FAILED."""

    app_url: str | None = None
    error_message: str | None = None
