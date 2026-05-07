"""Application settings and constants."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables.

    Matches the app.yaml valueFrom pattern used by the devops team:
    - DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (workspace auth)
    - POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_DB, POSTGRES_SCHEMA (Lakebase)
    - MODEL_SERVING_ENDPOINT (LLM)
    """

    # ─── Databricks Workspace ────────────────────────────────────────
    databricks_host: str = ""
    """Databricks workspace URL (e.g., https://dbc-xxx.cloud.databricks.com)."""

    databricks_http_path: str = ""
    """Databricks SQL warehouse HTTP path (if needed for SQL connector)."""

    databricks_token: str = ""
    """Personal access token (alternative to SP OAuth for local dev)."""

    databricks_client_id: str = ""
    """Service principal client ID for workspace authentication."""

    databricks_client_secret: str = ""
    """Service principal OAuth secret for workspace authentication."""

    databricks_catalog: str = "lakebase_accelerator_poc"
    """Unity Catalog name."""

    databricks_schema: str = "public"
    """Default schema in the catalog."""

    # ─── Model Serving ───────────────────────────────────────────────
    model_serving_endpoint: str = "databricks-claude-sonnet-4-5"
    """Model Serving endpoint name for LLM reasoning."""

    # ─── Lakebase / Postgres ─────────────────────────────────────────
    postgres_host: str = ""
    """Lakebase host (e.g., ep-orange-math-d2495qsd.database.us-east-1.cloud.databricks.com)."""

    postgres_port: int = 5432
    """Lakebase port."""

    postgres_user: str = ""
    """Lakebase Postgres role name (app name, e.g., 'lakebase-accelerator-app')."""

    postgres_db: str = "databricks_postgres"
    """Lakebase database name."""

    postgres_password: str = ""
    """Postgres password. For Databricks Apps, this is auto-injected via OAuth."""

    postgres_schema: str = "public"
    """Default schema for the accelerator's own metadata tables."""

    lakebase_endpoint_name: str = ""
    """Lakebase endpoint for OAuth token generation (optional).
    Format: projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>
    If set, uses SDK OAuth. If empty, uses POSTGRES_PASSWORD directly."""

    # ─── Volume Storage (Brownfield) ────────────────────────────────
    volume_catalog: str = "lakebase_poc_catalog_assets"
    """Unity Catalog name for the volume storing uploaded assets."""

    volume_schema: str = "asset_bundles"
    """Schema name within the volume catalog."""

    volume_name: str = "input_files"
    """Volume name for storing uploaded brownfield files."""

    # ─── Application ─────────────────────────────────────────────────
    app_name: str = "lakebase-accelerator"
    """Accelerator application name."""

    app_version: str = "1.0.0"
    """Application version."""

    catalog_name: str = "lakebase_accelerator_poc"
    """Unity Catalog name for generated apps."""

    # ─── Pipeline ────────────────────────────────────────────────────
    pipeline_timeout_seconds: int = 300
    """Maximum pipeline execution time in seconds."""

    deployment_poll_interval_seconds: int = 10
    """Interval between deployment status polls."""

    deployment_timeout_seconds: int = 300
    """Maximum time to wait for app deployment."""

    # ─── Frontend Build (Databricks Job) ─────────────────────────────
    frontend_build_node_type: str = "i3.xlarge"
    """Instance type for the frontend build cluster (single-node, needs ~4GB RAM for npm)."""

    frontend_build_spark_version: str = "14.3.x-scala2.12"
    """Databricks Runtime version for the frontend build cluster."""

    frontend_build_timeout_seconds: int = 300
    """Maximum time to wait for the frontend build job to complete."""

    frontend_build_use_serverless: bool = True
    """Use serverless compute for frontend builds (faster startup, no cluster needed).
    Set to False to use a classic single-node cluster instead."""

    # ─── LLM ─────────────────────────────────────────────────────────
    llm_max_retries: int = 2
    """Maximum retry attempts for LLM calls."""

    llm_timeout_seconds: int = 120
    """Timeout for individual LLM calls."""

    llm_temperature: float = 0.1
    """Default temperature for LLM calls."""

    llm_max_tokens: int = 8192
    """Default max tokens for LLM responses."""

    # ─── Connection Pool ─────────────────────────────────────────────
    db_pool_min_connections: int = 2
    """Minimum connections in the psycopg2 pool."""

    db_pool_max_connections: int = 10
    """Maximum connections in the psycopg2 pool."""

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    """Returns cached Settings singleton."""
    return Settings()


# ─── Constants ───────────────────────────────────────────────────────

# Schema naming
SCHEMA_PREFIX = "project_"
"""Prefix for all generated project schemas."""

SCHEMA_MAX_LENGTH = 63
"""PostgreSQL identifier max length."""

MAX_SCHEMA_NAME_LENGTH = 63
"""PostgreSQL identifier max length (alias)."""

SCHEMA_SUFFIX_LENGTH = 6
"""Hex suffix length for schema uniqueness."""

SCHEMA_MAX_COLLISION_RETRIES = 3
"""Max retries on schema name collision."""

# Accelerator internal schema
ACCELERATOR_META_SCHEMA = "accelerator_meta"
"""Schema name for accelerator metadata tables."""

# Required files in generated app bundle
REQUIRED_BACKEND_FILES = [
    "backend/main.py",
    "backend/requirements.txt",
    "backend/database.py",
    "backend/models.py",
]

REQUIRED_FRONTEND_FILES = [
    "frontend/package.json",
    "frontend/vite.config.ts",
    "frontend/tsconfig.json",
    "frontend/index.html",
    "frontend/src/main.tsx",
    "frontend/src/App.tsx",
]

# After a successful React build, frontend source files are replaced with built output.
# This is the minimal set of files expected in the deployed bundle.
REQUIRED_FRONTEND_BUILT_FILES = [
    "static/index.html",
]

REQUIRED_DEPLOYMENT_FILES = [
    "app.yaml",
    "Dockerfile",
]

REQUIRED_APP_FILES = REQUIRED_BACKEND_FILES + REQUIRED_FRONTEND_FILES + REQUIRED_DEPLOYMENT_FILES

# For validation after build: accepts either source OR built frontend
REQUIRED_APP_FILES_POST_BUILD = REQUIRED_BACKEND_FILES + REQUIRED_FRONTEND_BUILT_FILES + REQUIRED_DEPLOYMENT_FILES

# File upload constraints (brownfield)
MAX_UPLOAD_FILES = 10
MAX_UPLOAD_SIZE_BYTES = 50 * 1024 * 1024  # 50MB
ALLOWED_UPLOAD_EXTENSIONS = {
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
    ".md",
    ".txt",
}

# Prompt constraints
PROMPT_MIN_LENGTH = 1
PROMPT_MAX_LENGTH = 10_000

# Pipeline steps
GREENFIELD_STEPS = [
    "requirement_intake",
    "data_model_inference",
    "schema_provisioning",
    "seed_data_generation",
    "frontend_generation",
    "backend_generation",
    "deployment_config",
    "validation",
    "workspace_write",
    "app_deployment",
    "permission_grant",
    "audit",
]

BROWNFIELD_STEPS = [
    "prototype_ingestion",
    "data_model_inference",
    "schema_provisioning",
    "seed_data_generation",
    "frontend_generation",
    "backend_generation",
    "deployment_config",
    "validation",
    "workspace_write",
    "app_deployment",
    "permission_grant",
    "audit",
]

# Seed data
MIN_SEED_ROWS_PER_TABLE = 3

# Deployment retry
BACKOFF_BASE_SECONDS = 2
MAX_DEPLOYMENT_RETRIES = 3

# Workspace Files
WORKSPACE_APPS_BASE_PATH = "/Workspace/Apps"
