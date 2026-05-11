"""Prompt Service — business logic for managing and loading system prompts.

This service is the ONLY way to access prompts. It:
1. Loads prompts from the database via PromptRepository
2. Falls back to YAML file if DB is unavailable (graceful degradation)
3. Caches prompts in memory for performance
4. Provides seed functionality to populate DB from YAML on first boot

Services and nodes call this instead of reading YAML directly.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from lakebase_accelerator.repositories.prompt_repository import PromptRepository
from lakebase_accelerator.utils.logger import logger

# Path to the fallback YAML file (used when DB is unavailable)
_PROMPTS_YAML_PATH = Path(__file__).parent.parent / "resources" / "agent_prompts.yaml"

# In-memory cache for prompts (populated on first access)
_prompt_cache: dict[str, str] = {}
_cache_loaded: bool = False


class PromptService:
    """Service for loading and managing system prompts.

    Loads from DB first, falls back to YAML if DB unavailable.
    Caches all prompts in memory after first load.
    """

    def __init__(self, prompt_repo: PromptRepository) -> None:
        self._repo = prompt_repo

    def get_system_prompt(self, prompt_name: str, version: str = "v1") -> str:
        """Get a system prompt by name.

        Priority:
        1. In-memory cache (fastest)
        2. Database via PromptRepository (tries exact version, then any active)
        3. YAML file fallback (if DB unavailable)

        Args:
            prompt_name: The prompt identifier (e.g., 'frontend_generation').
            version: Version tag (default 'v1').

        Returns:
            The prompt text string.

        Raises:
            KeyError: If prompt not found in any source.
        """
        global _prompt_cache, _cache_loaded

        # 1. Check in-memory cache
        cache_key = f"{prompt_name}:{version}"
        if cache_key in _prompt_cache:
            return _prompt_cache[cache_key]

        # Also check without version (any version)
        cache_key_any = f"{prompt_name}:any"
        if cache_key_any in _prompt_cache:
            return _prompt_cache[cache_key_any]

        # 2. Try database — exact version first, then any active version
        prompt_data = self._repo.get_active_prompt(prompt_name, version)
        if not prompt_data:
            # Try without version constraint (get latest active)
            all_prompts = self._repo.get_all_active_prompts()
            prompt_data = next((p for p in all_prompts if p.get("prompt_name") == prompt_name), None)

        if prompt_data and prompt_data.get("prompt_text"):
            prompt_text = prompt_data["prompt_text"]
            _prompt_cache[cache_key] = prompt_text
            return prompt_text

        # 3. Fall back to YAML
        prompt_text = _load_from_yaml(prompt_name)
        if prompt_text:
            _prompt_cache[cache_key] = prompt_text
            logger.info(
                f"Prompt '{prompt_name}' loaded from YAML fallback (DB unavailable or empty)",
                extra={"prompt_name": prompt_name, "version": version},
            )
            return prompt_text

        raise KeyError(f"Prompt '{prompt_name}' not found in DB or YAML")

    def seed_prompts_from_yaml(self) -> int:
        """Seed the database with prompts from the YAML file.

        Called during application startup to ensure all prompts exist in DB.
        Only inserts prompts that don't already exist (won't overwrite DB edits).

        Returns:
            Number of prompts seeded.
        """
        yaml_prompts = _load_all_from_yaml()
        seeded = 0

        for prompt_name, config in yaml_prompts.items():
            version = config.get("version", "v1")
            system_text = config.get("system", "")

            if not system_text:
                continue

            # Only seed if not already in DB (check any version of this prompt)
            existing = self._repo.get_active_prompt(prompt_name, version)
            if existing:
                continue

            success = self._repo.upsert_prompt(
                prompt_name=prompt_name,
                prompt_type="system",
                prompt_text=system_text,
                version=version,
                description=f"Auto-seeded from agent_prompts.yaml ({version})",
            )
            if success:
                seeded += 1

        if seeded > 0:
            logger.info(f"Seeded {seeded} prompts from YAML to database")

        return seeded

    def refresh_cache(self) -> None:
        """Clear the in-memory cache, forcing next access to reload from DB."""
        global _prompt_cache
        _prompt_cache.clear()
        logger.info("Prompt cache cleared")

    def list_prompts(self) -> list[dict]:
        """List all active prompts from the database.

        Returns:
            List of prompt metadata dicts.
        """
        return self._repo.get_all_active_prompts()


def _load_from_yaml(prompt_name: str) -> str | None:
    """Load a single prompt from the YAML fallback file.

    Args:
        prompt_name: The prompt key in the YAML file.

    Returns:
        The system prompt string, or None if not found.
    """
    try:
        prompts = _load_all_from_yaml()
        config = prompts.get(prompt_name)
        if config:
            return config.get("system")
        return None
    except Exception as e:
        logger.warning(f"Failed to load prompt '{prompt_name}' from YAML: {e}")
        return None


@lru_cache
def _load_all_from_yaml() -> dict:
    """Load and cache all prompts from the YAML file."""
    try:
        with open(_PROMPTS_YAML_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load prompts YAML: {e}")
        return {}
