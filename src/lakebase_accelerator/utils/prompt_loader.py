"""Prompt loader utility — loads system prompts via PromptService.

Priority:
1. Database (via PromptService → PromptRepository)
2. YAML fallback (if DB unavailable)

This module provides the same interface as before (get_system_prompt)
but now routes through the service layer.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from lakebase_accelerator.utils.logger import logger

# Path to the fallback YAML file
PROMPTS_FILE = Path(__file__).parent.parent / "resources" / "agent_prompts.yaml"


def get_system_prompt(prompt_name: str) -> str:
    """Get a system prompt by name.

    Tries PromptService (DB) first, falls back to YAML if unavailable.

    Args:
        prompt_name: The prompt type key (e.g., 'frontend_generation').

    Returns:
        The system prompt string.

    Raises:
        KeyError: If prompt not found in any source.
    """
    # Try DB via PromptService
    try:
        from lakebase_accelerator.services.dependencies import get_prompt_service
        service = get_prompt_service()
        return service.get_system_prompt(prompt_name)
    except (RuntimeError, KeyError):
        # DB not available (pool not initialized) or prompt not in DB
        pass
    except Exception as e:
        logger.debug(f"PromptService unavailable, falling back to YAML: {e}")

    # Fallback to YAML
    return _get_from_yaml(prompt_name)


def get_prompt(prompt_type: str) -> dict:
    """Get a prompt configuration by type (legacy interface).

    Args:
        prompt_type: The prompt type key.

    Returns:
        Dict with 'version' and 'system' keys.

    Raises:
        KeyError: If prompt_type is not found.
    """
    prompts = _load_yaml()
    if prompt_type not in prompts:
        raise KeyError(f"Prompt type '{prompt_type}' not found in {PROMPTS_FILE.name}")
    return prompts[prompt_type]


def _get_from_yaml(prompt_name: str) -> str:
    """Load a prompt directly from YAML (fallback).

    Args:
        prompt_name: The prompt key.

    Returns:
        The system prompt string.

    Raises:
        KeyError: If not found in YAML.
    """
    prompts = _load_yaml()
    if prompt_name not in prompts:
        raise KeyError(f"Prompt '{prompt_name}' not found in {PROMPTS_FILE.name}")
    return prompts[prompt_name]["system"]


@lru_cache
def _load_yaml() -> dict:
    """Load and cache the agent prompts YAML file."""
    with open(PROMPTS_FILE) as f:
        return yaml.safe_load(f)
