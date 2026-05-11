"""System Prompt table schema — stores reusable LLM prompt templates.

These prompts are loaded by the PromptService and used by pipeline nodes
instead of hardcoded YAML strings. Prompts can be versioned and toggled
via is_active flag without code deployments.
"""

from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA

TABLE_NAME = "system_prompts"
FULL_TABLE_NAME = f"{ACCELERATOR_META_SCHEMA}.{TABLE_NAME}"

# DDL for table creation (used by schema_init.py)
CREATE_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {FULL_TABLE_NAME} (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_name     VARCHAR(100) NOT NULL,
    prompt_type     VARCHAR(50) NOT NULL,
    prompt_version  VARCHAR(10) NOT NULL DEFAULT 'v1',
    prompt_text     TEXT NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255) NOT NULL DEFAULT 'sp_accelerator',
    modified_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by     VARCHAR(255) NOT NULL DEFAULT 'sp_accelerator'
)
"""

# Indexes
CREATE_INDEXES_DDL = [
    f"CREATE INDEX IF NOT EXISTS idx_system_prompts_name ON {FULL_TABLE_NAME}(prompt_name)",
    f"CREATE INDEX IF NOT EXISTS idx_system_prompts_type ON {FULL_TABLE_NAME}(prompt_type)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_system_prompts_name_version ON {FULL_TABLE_NAME}(prompt_name, prompt_version) WHERE is_active = true",
]

# Column definitions for reference
COLUMNS = {
    "id": "UUID PK",
    "prompt_name": "VARCHAR(100) — unique identifier (e.g., 'frontend_generation', 'requirement_intake')",
    "prompt_type": "VARCHAR(50) — category (e.g., 'system', 'user_template')",
    "prompt_version": "VARCHAR(10) — version tag (e.g., 'v1', 'v2')",
    "prompt_text": "TEXT — the actual prompt content",
    "description": "TEXT — human-readable description of what this prompt does",
    "is_active": "BOOLEAN — only active prompts are loaded",
    "created_at": "TIMESTAMPTZ",
    "created_by": "VARCHAR(255)",
    "modified_at": "TIMESTAMPTZ",
    "modified_by": "VARCHAR(255)",
}
