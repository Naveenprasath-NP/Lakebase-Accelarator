"""Enumerations for pipeline steps, statuses, column types, and cardinality."""

from enum import Enum


class PipelineStep(str, Enum):
    """Identifies each step in the pipeline."""

    REQUIREMENT_INTAKE = "requirement_intake"
    PROTOTYPE_INGESTION = "prototype_ingestion"
    DATA_MODEL_INFERENCE = "data_model_inference"
    SCHEMA_PROVISIONING = "schema_provisioning"
    SEED_DATA_GENERATION = "seed_data_generation"
    FRONTEND_GENERATION = "frontend_generation"
    BACKEND_GENERATION = "backend_generation"
    DEPLOYMENT_CONFIG = "deployment_config"
    VALIDATION = "validation"
    WORKSPACE_WRITE = "workspace_write"
    APP_DEPLOYMENT = "app_deployment"
    PERMISSION_GRANT = "permission_grant"
    AUDIT = "audit"


class PipelineStatus(str, Enum):
    """Overall pipeline execution status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    """Individual step execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProjectMode(str, Enum):
    """Pipeline mode — greenfield or brownfield."""

    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"


class ColumnType(str, Enum):
    """Supported PostgreSQL column types for Lakebase."""

    UUID = "UUID"
    TEXT = "TEXT"
    VARCHAR = "VARCHAR"
    INTEGER = "INTEGER"
    BIGINT = "BIGINT"
    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    TIMESTAMP = "TIMESTAMP"
    TIMESTAMPTZ = "TIMESTAMPTZ"
    DATE = "DATE"
    JSONB = "JSONB"
    SERIAL = "SERIAL"


class Cardinality(str, Enum):
    """Relationship cardinality between entities."""

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_MANY = "many_to_many"
