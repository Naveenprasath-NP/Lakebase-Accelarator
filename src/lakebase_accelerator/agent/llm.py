"""Shared LLM instance for all agents.

Uses ChatOpenAI pointed at Databricks Model Serving (OpenAI-compatible).
Handles OAuth token generation for authentication.
Provides instrumented invocation with token usage logging.
"""

import time
from typing import Any

import httpx
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from lakebase_accelerator.services.dependencies import get_model_consumption_service
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


def invoke_with_logging(
    llm: ChatOpenAI,
    messages: list[BaseMessage],
    project_id: str,
    call_type: str,
) -> Any:
    """Invoke the LLM and log token consumption metrics.

    Wraps a standard llm.invoke() call with latency measurement and
    token usage extraction. Logs consumption to the model_consumption
    table via ModelConsumptionService (fire-and-forget).

    Args:
        llm: The ChatOpenAI instance to invoke.
        messages: List of LangChain messages to send.
        project_id: UUID of the project this call belongs to.
        call_type: Type of LLM call (e.g., 'intake', 'exploration', 'generation').

    Returns:
        The LangChain AIMessage response from the LLM.
    """
    settings = get_settings()

    start_time = time.time()
    response = llm.invoke(messages)
    end_time = time.time()

    latency_ms = (end_time - start_time) * 1000

    # Extract token usage from response metadata
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0

    usage_metadata = getattr(response, "usage_metadata", None)
    if usage_metadata and isinstance(usage_metadata, dict):
        input_tokens = usage_metadata.get("input_tokens", 0)
        output_tokens = usage_metadata.get("output_tokens", 0)
        total_tokens = usage_metadata.get("total_tokens", 0)

    # Fire-and-forget: log consumption without blocking the response
    try:
        consumption_service = get_model_consumption_service()
        consumption_service.log_consumption(
            project_id=project_id,
            model_endpoint=settings.model_serving_endpoint,
            model_name=settings.model_serving_endpoint,
            call_type=call_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            status="success",
        )
    except Exception as e:
        # Fire-and-forget: never let logging failures affect the LLM response
        logger.warning(
            f"Failed to log model consumption: {e}",
            extra={
                "project_id": project_id,
                "call_type": call_type,
                "latency_ms": latency_ms,
                "error": str(e),
            },
        )

    return response
