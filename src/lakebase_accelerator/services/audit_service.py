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
        uploaded_files: list[dict] | None = None,
    ) -> str:
        """Create a project record at pipeline start.

        Returns:
            The generated project UUID.
        """
        import json as _json

        logger.info(
            "Creating project record",
            extra={"step": "audit", "project_name": project_name, "mode": mode},
        )

        files_json = _json.dumps(uploaded_files) if uploaded_files else "[]"

        rows = self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            INSERT INTO accelerator_meta.projects
                (project_name, schema_name, mode, prompt, status, uploaded_files)
            VALUES (%s, %s, %s, %s, 'in_progress', %s::jsonb)
            RETURNING id::text
            """,
            (project_name, schema_name, mode, prompt, files_json),
        )

        project_id = rows[0]["id"] if rows else "unknown"
        logger.info(f"Project record created: {project_id}", extra={"step": "audit"})
        return project_id

    async def update_project_completed(
        self,
        project_id: str,
        app_name: str,
        app_url: str,
        tables_created: list[str],
        pipeline_duration_seconds: float,
        schema_name: str = "",
        project_name: str = "",
    ) -> None:
        """Update project record on successful completion."""
        logger.info(
            "Updating project record: completed",
            extra={"step": "audit", "project_id": project_id},
        )

        set_parts = [
            "status = 'completed'",
            "app_name = %s",
            "app_url = %s",
            "schema_name = %s",
            "generated_tables = %s::jsonb",
            "pipeline_duration_seconds = %s",
            "modified_at = NOW()",
        ]
        params: list = [
            app_name,
            app_url,
            schema_name,
            str(tables_created).replace("'", '"'),
            pipeline_duration_seconds,
        ]

        if project_name:
            set_parts.append("project_name = %s")
            params.append(project_name)

        params.append(project_id)

        self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            f"""
            UPDATE accelerator_meta.projects
            SET {', '.join(set_parts)}
            WHERE id = %s::uuid
            """,
            tuple(params),
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
            SELECT id::text, project_name, mode, status, prompt, app_url,
                   schema_name, generated_tables, created_at
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
                   schema_name, generated_tables, uploaded_files, chat_history,
                   pipeline_duration_seconds,
                   failure_step, failure_message,
                   created_at, modified_at
            FROM accelerator_meta.projects
            WHERE id = %s::uuid
            """,
            (project_id,),
        )
        return rows[0] if rows else None

    async def append_chat_message(
        self,
        project_id: str,
        role: str,
        content: str,
        message_type: str = "message",
    ) -> None:
        """Append a chat message to the project's chat_history.

        Args:
            project_id: UUID of the project.
            role: "user" or "agent".
            content: Message content (text or markdown).
            message_type: Type of message (message, checkpoint_summary, checkpoint_approval, pipeline_complete).
        """
        import json as _json
        from datetime import datetime, UTC

        message = _json.dumps({
            "role": role,
            "content": content,
            "type": message_type,
            "timestamp": datetime.now(UTC).isoformat(),
        })

        self._repo.execute_query(
            ACCELERATOR_META_SCHEMA,
            """
            UPDATE accelerator_meta.projects
            SET chat_history = chat_history || %s::jsonb,
                modified_at = NOW()
            WHERE id = %s::uuid
            """,
            (message, project_id),
        )
