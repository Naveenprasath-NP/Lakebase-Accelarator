"""Shared LLM instance for all agents.

Uses ChatOpenAI pointed at Databricks Model Serving (OpenAI-compatible).
Handles OAuth token generation for authentication.
"""

import httpx
from langchain_openai import ChatOpenAI

from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger

_cached_token: str | None = None


def get_llm(max_tokens: int = 4096) -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at Databricks Model Serving.

    Args:
        max_tokens: Max tokens for the response.

    Returns:
        ChatOpenAI configured for Databricks.
    """
    settings = get_settings()
    token = _get_workspace_token(settings)
    base_url = f"{settings.databricks_host}/serving-endpoints"

    return ChatOpenAI(
        model=settings.model_serving_endpoint,
        base_url=base_url,
        api_key=token,
        temperature=0.1,
        max_tokens=max_tokens,
        streaming=False,
        stream_usage=False,
        disable_streaming=True,
    )


def _get_workspace_token(settings) -> str:
    """Get workspace OAuth token using SP credentials (cached)."""
    global _cached_token
    if _cached_token:
        return _cached_token

    token_url = f"{settings.databricks_host}/oidc/v1/token"
    response = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        auth=(settings.databricks_client_id, settings.databricks_client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Failed to get workspace token: {response.status_code}")

    _cached_token = response.json()["access_token"]
    logger.info("Workspace OAuth token acquired for LLM")
    return _cached_token


def reset_token_cache() -> None:
    """Reset the cached token (call on 401 errors)."""
    global _cached_token
    _cached_token = None
