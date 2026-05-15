"""Shared LLM instance for all agents.

Uses ChatOpenAI pointed at Databricks Model Serving (OpenAI-compatible).
Handles OAuth token generation for authentication.
Provides instrumented invocation with token usage logging.
"""

import time
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from lakebase_accelerator.services.dependencies import get_model_consumption_service
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger

_cached_token: str | None = None
_token_expiry: float = 0  # Unix timestamp when token expires


def get_llm(max_tokens: int = 4096, project_id: str = "", call_type: str = "pipeline") -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at Databricks Model Serving.

    Includes a callback that automatically logs token consumption for every
    invoke() call, so individual nodes don't need to use invoke_with_logging().

    Args:
        max_tokens: Max tokens for the response.
        project_id: Project ID for consumption logging (optional).
        call_type: Type of call for logging (optional).

    Returns:
        ChatOpenAI configured for Databricks with auto-logging callback.
    """
    settings = get_settings()
    token = _get_workspace_token(settings)
    base_url = f"{settings.databricks_host}/serving-endpoints"

    llm = ChatOpenAI(
        model=settings.model_serving_endpoint,
        base_url=base_url,
        api_key=token,
        temperature=0.1,
        max_tokens=max_tokens,
        streaming=False,
        stream_usage=False,
        disable_streaming=True,
        callbacks=[_ConsumptionLoggingCallback(project_id, call_type, settings.model_serving_endpoint)],
    )
    return llm


def _get_workspace_token(settings) -> str:
    """Get workspace OAuth token using SP credentials.

    Caches the token and refreshes it 5 minutes before expiry (tokens last 60 min).
    """
    global _cached_token, _token_expiry

    # Return cached token if still valid (with 5 min buffer)
    if _cached_token and time.time() < (_token_expiry - 300):
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

    token_data = response.json()
    _cached_token = token_data["access_token"]
    # Token typically expires in 3600s (1 hour)
    expires_in = token_data.get("expires_in", 3600)
    _token_expiry = time.time() + expires_in

    logger.info(f"Workspace OAuth token acquired for LLM (expires in {expires_in}s)")
    return _cached_token


def reset_token_cache() -> None:
    """Reset the cached token (call on 401 errors)."""
    global _cached_token, _token_expiry
    _cached_token = None
    _token_expiry = 0


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


# ═══════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER — Auto-logs token consumption for every LLM call
# ═══════════════════════════════════════════════════════════════════════


class _ConsumptionLoggingCallback(BaseCallbackHandler):
    """LangChain callback that logs token consumption after every LLM call.

    This is attached to every ChatOpenAI instance created by get_llm(),
    so ALL nodes automatically get consumption logging without needing
    to use invoke_with_logging() explicitly.
    """

    def __init__(self, project_id: str, call_type: str, model_endpoint: str) -> None:
        self._project_id = project_id
        self._call_type = call_type
        self._model_endpoint = model_endpoint
        self._start_time: float = 0

    def on_llm_start(self, *args, **kwargs) -> None:
        """Record start time when LLM call begins."""
        self._start_time = time.time()

    def on_llm_end(self, response, **kwargs) -> None:
        """Log token consumption when LLM call completes."""
        latency_ms = (time.time() - self._start_time) * 1000 if self._start_time else 0

        # Extract token usage from LLM result
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0

        if hasattr(response, "llm_output") and response.llm_output:
            token_usage = response.llm_output.get("token_usage", {})
            input_tokens = token_usage.get("prompt_tokens", 0)
            output_tokens = token_usage.get("completion_tokens", 0)
            total_tokens = token_usage.get("total_tokens", 0)

        # Fire-and-forget logging
        try:
            service = get_model_consumption_service()
            service.log_consumption(
                project_id=self._project_id if self._project_id else None,
                model_endpoint=self._model_endpoint,
                model_name=self._model_endpoint,
                call_type=self._call_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                status="success",
            )
        except Exception as e:
            logger.debug(f"Consumption callback logging failed (non-fatal): {e}")

    def on_llm_error(self, error, **kwargs) -> None:
        """Log failed LLM calls."""
        latency_ms = (time.time() - self._start_time) * 1000 if self._start_time else 0

        try:
            service = get_model_consumption_service()
            service.log_consumption(
                project_id=self._project_id or "00000000-0000-0000-0000-000000000000",
                model_endpoint=self._model_endpoint,
                model_name=self._model_endpoint,
                call_type=self._call_type,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=latency_ms,
                status="error",
            )
        except Exception:
            pass
