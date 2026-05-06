"""Audit Service — Step 12 of both pipelines.

Records pipeline execution metadata to the accelerator_meta schema
for tracking, history, and observability.
"""

from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA
from lakebase_accelerator.utils.logger import logger


class AuditService:
    """Records pipeline execution metadata to accelerator_meta."""

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    async def create_project_record(
        self,
        project_name: str,
        schema_name: str,
        mode: str,
        prompt: str,
    ) -> str:
        """Create a project record at pipeline start.

        Returns:
            The generated project UUID.
        """
        logger.info(
            "Creating project record",
            extra={"step": "audit", "project_name": project_name, "mode": mode},
        )

        rows = self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            INSERT INTO accelerator_meta.projects
                (project_name, schema_name, mode, prompt, status)
            VALUES (%s, %s, %s, %s, 'in_progress')
            RETURNING id::text
            """,
            (project_name, schema_name, mode, prompt),
        )

        project_id = rows[0]["id"] if rows else "unknown"
        logger.info(f"Project record created: {project_id}", extra={"step": "audit"})
        return project_id

    async def update_project_completed(
        self,
        project_id: str,
        app_name: str,
        app_url: str,
        service_principal_id: str,
        tables_created: list[str],
        pipeline_duration_seconds: float,
        total_token_usage: int,
    ) -> None:
        """Update project record on successful completion."""
        logger.info(
            "Updating project record: completed",
            extra={"step": "audit", "project_id": project_id},
        )

        self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            UPDATE accelerator_meta.projects
            SET status = 'completed',
                app_name = %s,
                app_url = %s,
                service_principal_id = %s,
                generated_tables = %s::jsonb,
                pipeline_duration_seconds = %s,
                total_token_usage = %s,
                modified_at = NOW()
            WHERE id = %s::uuid
            """,
            (
                app_name,
                app_url,
                service_principal_id,
                str(tables_created).replace("'", '"'),
                pipeline_duration_seconds,
                total_token_usage,
                project_id,
            ),
        )

    async def update_project_failed(
        self,
        project_id: str,
        failure_step: str,
        failure_message: str,
    ) -> None:
        """Update project record on pipeline failure."""
        logger.info(
            "Updating project record: failed",
            extra={"step": "audit", "project_id": project_id, "failure_step": failure_step},
        )

        self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            UPDATE accelerator_meta.projects
            SET status = 'failed',
                failure_step = %s,
                failure_message = %s,
                modified_at = NOW()
            WHERE id = %s::uuid
            """,
            (failure_step, failure_message, project_id),
        )

    async def get_all_projects(
        self,
        mode: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Fetch project list for the history API.

        Returns:
            Tuple of (project_rows, total_count).
        """
        conditions = []
        params: list = []

        if mode:
            conditions.append("mode = %s")
            params.append(mode)
        if status:
            conditions.append("status = %s")
            params.append(status)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Get total count
        count_rows = self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            f"SELECT COUNT(*) as total FROM accelerator_meta.projects {where_clause}",
            tuple(params) if params else None,
        )
        total = count_rows[0]["total"] if count_rows else 0

        # Get paginated results
        params.extend([limit, offset])
        rows = self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            f"""
            SELECT id::text, project_name, mode, status, prompt, app_url, created_at
            FROM accelerator_meta.projects
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params),
        )

        return rows, total

    async def get_project_detail(self, project_id: str) -> dict | None:
        """Fetch full project detail for the detail API."""
        rows = self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            SELECT id::text, project_name, mode, status, prompt, app_name, app_url,
                   schema_name, generated_tables, pipeline_duration_seconds,
                   total_token_usage, failure_step, failure_message,
                   created_at, modified_at
            FROM accelerator_meta.projects
            WHERE id = %s::uuid
            """,
            (project_id,),
        )
        return rows[0] if rows else None
