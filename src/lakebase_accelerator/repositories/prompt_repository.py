"""Prompt Repository — data access layer for system_prompts table.

All database operations for prompt management go through this repository.
Services NEVER access the DB directly — they use this repository.
"""

from lakebase_accelerator.repositories.lakebase_repository import LakebaseRepository
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA
from lakebase_accelerator.utils.logger import logger


class PromptRepository:
    """Repository for system_prompts table operations.

    Handles all CRUD operations for prompt templates stored in the database.
    """

    def __init__(self, lakebase_repo: LakebaseRepository) -> None:
        self._repo = lakebase_repo

    def get_active_prompt(self, prompt_name: str, version: str = "v1") -> dict | None:
        """Get an active prompt by name and version.

        Args:
            prompt_name: The prompt identifier (e.g., 'frontend_generation').
            version: The prompt version (default 'v1').

        Returns:
            Dict with prompt data or None if not found.
        """
        try:
            rows = self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                SELECT id, prompt_name, prompt_type, prompt_version, prompt_text, description
                FROM accelerator_meta.system_prompts
                WHERE prompt_name = %s AND prompt_version = %s AND is_active = true
                LIMIT 1
                """,
                (prompt_name, version),
            )
            return rows[0] if rows else None
        except Exception as e:
            logger.warning(f"Failed to fetch prompt '{prompt_name}' v{version} from DB: {e}")
            return None

    def get_all_active_prompts(self) -> list[dict]:
        """Get all active prompts (latest version per name).

        Returns:
            List of prompt dicts.
        """
        try:
            return self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                SELECT DISTINCT ON (prompt_name)
                    id, prompt_name, prompt_type, prompt_version, prompt_text, description
                FROM accelerator_meta.system_prompts
                WHERE is_active = true
                ORDER BY prompt_name, prompt_version DESC
                """,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch all active prompts from DB: {e}")
            return []

    def upsert_prompt(
        self,
        prompt_name: str,
        prompt_type: str,
        prompt_text: str,
        version: str = "v1",
        description: str | None = None,
    ) -> bool:
        """Insert or update a prompt in the database.

        If a prompt with the same name and version exists, updates it.
        Otherwise, inserts a new record.

        Args:
            prompt_name: The prompt identifier.
            prompt_type: Category (e.g., 'system', 'user_template').
            prompt_text: The actual prompt content.
            version: Version tag.
            description: Human-readable description.

        Returns:
            True if successful, False on failure.
        """
        try:
            self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                INSERT INTO accelerator_meta.system_prompts
                    (prompt_name, prompt_type, prompt_version, prompt_text, description)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (prompt_name, prompt_version) WHERE is_active = true
                DO UPDATE SET
                    prompt_text = EXCLUDED.prompt_text,
                    prompt_type = EXCLUDED.prompt_type,
                    description = EXCLUDED.description,
                    modified_at = NOW()
                """,
                (prompt_name, prompt_type, version, prompt_text, description),
            )
            logger.info(f"Prompt upserted: {prompt_name} v{version}")
            return True
        except Exception as e:
            logger.warning(f"Failed to upsert prompt '{prompt_name}': {e}")
            return False

    def deactivate_prompt(self, prompt_name: str, version: str = "v1") -> bool:
        """Deactivate a prompt (soft delete).

        Args:
            prompt_name: The prompt identifier.
            version: Version to deactivate.

        Returns:
            True if successful, False on failure.
        """
        try:
            self._repo.execute_query(
                ACCELERATOR_META_SCHEMA,
                """
                UPDATE accelerator_meta.system_prompts
                SET is_active = false, modified_at = NOW()
                WHERE prompt_name = %s AND prompt_version = %s
                """,
                (prompt_name, version),
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to deactivate prompt '{prompt_name}': {e}")
            return False
