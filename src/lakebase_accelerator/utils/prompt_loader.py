"""YAML prompt loader utility for agent system prompts."""

from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_FILE = Path(__file__).parent.parent / "resources" / "agent_prompts.yaml"


@lru_cache
def _load_prompts() -> dict:
    """Load and cache the agent prompts YAML file."""
    with open(PROMPTS_FILE) as f:
        return yaml.safe_load(f)


def get_prompt(prompt_type: str) -> dict:
    """Get a prompt configuration by type.

    Args:
        prompt_type: The prompt type key (e.g., 'requirement_intake').

    Returns:
        Dict with 'version' and 'system' keys.

    Raises:
        KeyError: If prompt_type is not found in the YAML file.
    """
    prompts = _load_prompts()
    if prompt_type not in prompts:
        raise KeyError(f"Prompt type '{prompt_type}' not found in {PROMPTS_FILE.name}")
    return prompts[prompt_type]


def get_system_prompt(prompt_type: str) -> str:
    """Get just the system prompt string for a given type.

    Args:
        prompt_type: The prompt type key.

    Returns:
        The system prompt string.
    """
    return get_prompt(prompt_type)["system"]
