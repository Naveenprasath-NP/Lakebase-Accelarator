"""Model for generated application file collections."""

from pydantic import BaseModel, ConfigDict, Field


class GeneratedFiles(BaseModel):
    """Collection of generated source files."""

    model_config = ConfigDict(frozen=True)

    files: dict[str, str] = Field(default_factory=dict)
    """Mapping of relative file path to file content."""
