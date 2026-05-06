"""LLM response models for requirement intake and prototype ingestion."""

from pydantic import BaseModel, ConfigDict, Field

from lakebase_accelerator.models.enums import Cardinality, ColumnType


class EntityAttribute(BaseModel):
    """A single attribute/column of a business entity."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Column name (snake_case)."""

    data_type: ColumnType
    """PostgreSQL data type."""

    nullable: bool = True
    """Whether the column allows NULL values."""

    is_primary_key: bool = False
    """Whether this column is the primary key."""

    default_value: str | None = None
    """Default value expression (e.g., 'gen_random_uuid()', 'NOW()')."""

    max_length: int | None = None
    """Max length for VARCHAR columns."""


class EntityRelationship(BaseModel):
    """A relationship between two business entities."""

    model_config = ConfigDict(frozen=True)

    from_entity: str
    """Source entity name."""

    to_entity: str
    """Target entity name."""

    cardinality: Cardinality
    """Relationship cardinality."""

    foreign_key_column: str
    """FK column name on the source entity."""

    on_delete: str = "CASCADE"
    """ON DELETE behavior (CASCADE, SET NULL, RESTRICT)."""


class WorkflowStep(BaseModel):
    """A step in an inferred business workflow."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Step name."""

    description: str
    """What happens in this step."""

    involved_entities: list[str] = Field(default_factory=list)
    """Entities involved in this workflow step."""


class EntityDefinition(BaseModel):
    """A complete business entity with attributes."""

    model_config = ConfigDict(frozen=True)

    name: str
    """Entity name (snake_case, singular)."""

    description: str
    """Business description of this entity."""

    attributes: list[EntityAttribute]
    """List of columns/attributes."""


class AnalysisResult(BaseModel):
    """Structured output from the requirement intake LLM call (greenfield)."""

    model_config = ConfigDict(frozen=True)

    project_name: str
    """Inferred project name (kebab-case)."""

    domain_summary: str
    """Brief summary of the business domain."""

    entities: list[EntityDefinition]
    """Inferred business entities with attributes."""

    relationships: list[EntityRelationship] = Field(default_factory=list)
    """Relationships between entities."""

    workflows: list[WorkflowStep] = Field(default_factory=list)
    """Inferred business workflows."""

    clarification_questions: list[str] = Field(default_factory=list)
    """Questions to ask the user if the prompt is ambiguous."""


class ProductionGap(BaseModel):
    """A production gap identified in a prototype."""

    model_config = ConfigDict(frozen=True)

    category: str
    """Gap category (backend, validation, auth, error_handling, database)."""

    description: str
    """Description of the gap."""

    severity: str
    """Severity: critical, high, medium, low."""


class PrototypeAnalysis(BaseModel):
    """Structured output from the prototype ingestion LLM call (brownfield)."""

    model_config = ConfigDict(frozen=True)

    project_name: str
    """Inferred project name (kebab-case)."""

    domain_summary: str
    """Brief summary of the business domain."""

    entities: list[EntityDefinition]
    """Inferred business entities with attributes."""

    relationships: list[EntityRelationship] = Field(default_factory=list)
    """Relationships between entities."""

    workflows: list[WorkflowStep] = Field(default_factory=list)
    """Inferred business workflows."""

    production_gaps: list[ProductionGap] = Field(default_factory=list)
    """Identified production gaps in the prototype."""

    migration_notes: str = ""
    """Notes on how to migrate the prototype to production."""
