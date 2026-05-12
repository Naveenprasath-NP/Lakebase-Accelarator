"""Accelerator meta schema initialization.

Creates the accelerator_meta schema and tables on first boot if they don't exist.
This schema stores project records, pipeline execution history, and audit logs.
"""

from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA
from lakebase_accelerator.utils.logger import logger

DDL_STATEMENTS = [
    f"CREATE SCHEMA IF NOT EXISTS {ACCELERATOR_META_SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {ACCELERATOR_META_SCHEMA}.projects (
        id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_name            VARCHAR(100) NOT NULL DEFAULT '',
        schema_name             VARCHAR(63) NOT NULL DEFAULT '',
        app_name                VARCHAR(100),
        app_url                 TEXT,
        status                  VARCHAR(20) NOT NULL DEFAULT 'pending',
        mode                    VARCHAR(20) NOT NULL DEFAULT 'greenfield',
        prompt                  TEXT NOT NULL,
        generated_tables        JSONB,
        chat_history            JSONB NOT NULL DEFAULT '[]'::jsonb,
        pipeline_duration_seconds FLOAT,
        failure_step            TEXT,
        failure_message         TEXT,
        is_active               BOOLEAN NOT NULL DEFAULT true,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        modified_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACCELERATOR_META_SCHEMA}.model_consumption (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id          UUID REFERENCES {ACCELERATOR_META_SCHEMA}.projects(id) ON DELETE SET NULL,
        model_endpoint      VARCHAR(100) NOT NULL,
        model_name          VARCHAR(100),
        call_type           VARCHAR(50) NOT NULL,
        input_tokens        INTEGER NOT NULL DEFAULT 0,
        output_tokens       INTEGER NOT NULL DEFAULT 0,
        total_tokens        INTEGER NOT NULL DEFAULT 0,
        latency_ms          FLOAT NOT NULL,
        status              VARCHAR(20) NOT NULL,
        is_active           BOOLEAN NOT NULL DEFAULT true,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACCELERATOR_META_SCHEMA}.error_logs (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id          UUID REFERENCES {ACCELERATOR_META_SCHEMA}.projects(id) ON DELETE SET NULL,
        error_code          VARCHAR(50) NOT NULL,
        severity            VARCHAR(20) NOT NULL,
        source_component    VARCHAR(100) NOT NULL,
        source_step         VARCHAR(50),
        error_message       TEXT NOT NULL,
        stack_trace         TEXT,
        is_active           BOOLEAN NOT NULL DEFAULT true,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACCELERATOR_META_SCHEMA}.system_prompts (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        prompt_name     VARCHAR(100) NOT NULL,
        prompt_type     VARCHAR(50) NOT NULL DEFAULT 'system',
        prompt_version  VARCHAR(10) NOT NULL DEFAULT 'v1',
        prompt_text     TEXT NOT NULL,
        description     TEXT,
        is_active       BOOLEAN NOT NULL DEFAULT true,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        modified_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # Indexes
    f"CREATE INDEX IF NOT EXISTS idx_projects_status ON {ACCELERATOR_META_SCHEMA}.projects(status)",
    f"CREATE INDEX IF NOT EXISTS idx_projects_mode ON {ACCELERATOR_META_SCHEMA}.projects(mode)",
    f"CREATE INDEX IF NOT EXISTS idx_projects_created_at ON {ACCELERATOR_META_SCHEMA}.projects(created_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_model_consumption_project ON {ACCELERATOR_META_SCHEMA}.model_consumption(project_id)",
    f"CREATE INDEX IF NOT EXISTS idx_error_logs_project ON {ACCELERATOR_META_SCHEMA}.error_logs(project_id)",
    f"CREATE INDEX IF NOT EXISTS idx_system_prompts_name ON {ACCELERATOR_META_SCHEMA}.system_prompts(prompt_name)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_system_prompts_name_version ON {ACCELERATOR_META_SCHEMA}.system_prompts(prompt_name, prompt_version) WHERE is_active = true",
]


def initialize_accelerator_schema(connection_pool) -> None:
    """Create accelerator_meta schema and tables if they don't exist.

    Called once during application startup (lifespan).
    Accepts either a LakebaseConnectionPool or a psycopg2 ThreadedConnectionPool.
    """
    if connection_pool is None:
        logger.warning("No connection pool — skipping schema initialization")
        return

    # Support both our custom pool and raw psycopg2 pool
    if hasattr(connection_pool, "getconn"):
        conn = connection_pool.getconn()
    else:
        logger.warning("Unknown pool type — skipping schema initialization")
        return

    try:
        with conn.cursor() as cur:
            for ddl in DDL_STATEMENTS:
                cur.execute(ddl)
        conn.commit()
        logger.info(f"Schema '{ACCELERATOR_META_SCHEMA}' initialized successfully")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to initialize accelerator_meta schema: {e}")
        raise
    finally:
        if hasattr(connection_pool, "putconn"):
            connection_pool.putconn(conn)
