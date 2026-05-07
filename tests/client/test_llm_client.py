"""Tests for LLMClient with retry and structured output parsing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from lakebase_accelerator.client.llm_client import LLMClient
from lakebase_accelerator.utils.exceptions import LLMClientError


class SampleResponseModel(BaseModel):
    """Sample Pydantic model for testing validation."""

    name: str
    age: int


@pytest.fixture
def mock_workspace_client() -> MagicMock:
    """Create a mock WorkspaceClient."""
    client = MagicMock()
    return client


@pytest.fixture
def llm_client(mock_workspace_client: MagicMock) -> LLMClient:
    """Create an LLMClient instance with mocked workspace client."""
    return LLMClient(
        workspace_client=mock_workspace_client,
        endpoint_name="test-endpoint",
        max_retries=2,
        timeout_seconds=30,
    )


class TestLLMClientCall:
    """Tests for LLMClient.call() method."""

    async def test_call_returns_parsed_dict(self, llm_client: LLMClient) -> None:
        """Successful call returns parsed dict when no response_model provided."""
        expected = {"key": "value", "count": 42}
        raw_response = json.dumps(expected)

        with patch.object(llm_client, "_invoke_endpoint", new_callable=AsyncMock, return_value=raw_response):
            result = await llm_client.call(
                system_prompt="You are a helper.",
                user_prompt="Return JSON.",
            )

        assert result == expected

    async def test_call_with_pydantic_model_validation(self, llm_client: LLMClient) -> None:
        """Successful call with Pydantic model returns validated model instance."""
        raw_response = json.dumps({"name": "Alice", "age": 30})

        with patch.object(llm_client, "_invoke_endpoint", new_callable=AsyncMock, return_value=raw_response):
            result = await llm_client.call(
                system_prompt="You are a helper.",
                user_prompt="Return a person.",
                response_model=SampleResponseModel,
            )

        assert isinstance(result, SampleResponseModel)
        assert result.name == "Alice"
        assert result.age == 30

    async def test_retry_on_json_parse_error(self, llm_client: LLMClient) -> None:
        """Retries on malformed JSON and succeeds on subsequent attempt."""
        valid_response = json.dumps({"status": "ok"})

        with patch.object(
            llm_client,
            "_invoke_endpoint",
            new_callable=AsyncMock,
            side_effect=["not valid json {{{", valid_response],
        ):
            result = await llm_client.call(
                system_prompt="System",
                user_prompt="User",
            )

        assert result == {"status": "ok"}

    async def test_retry_on_pydantic_validation_error_appends_context(self, llm_client: LLMClient) -> None:
        """Retries on Pydantic validation error and appends error context to prompt."""
        # First response missing required 'age' field, second response is valid
        invalid_response = json.dumps({"name": "Bob"})
        valid_response = json.dumps({"name": "Bob", "age": 25})

        invoke_mock = AsyncMock(side_effect=[invalid_response, valid_response])

        with patch.object(llm_client, "_invoke_endpoint", invoke_mock):
            result = await llm_client.call(
                system_prompt="System",
                user_prompt="Return a person.",
                response_model=SampleResponseModel,
            )

        assert isinstance(result, SampleResponseModel)
        assert result.name == "Bob"
        assert result.age == 25

        # Verify the second call included error context in the prompt
        second_call_args = invoke_mock.call_args_list[1]
        assert "Previous response had an error" in second_call_args.kwargs["user_prompt"]

    async def test_raises_llm_client_error_after_max_retries(self, llm_client: LLMClient) -> None:
        """Raises LLMClientError after all retries are exhausted."""
        with (
            patch.object(
                llm_client,
                "_invoke_endpoint",
                new_callable=AsyncMock,
                side_effect=["invalid json", "still invalid", "nope"],
            ),
            pytest.raises(LLMClientError) as exc_info,
        ):
            await llm_client.call(
                system_prompt="System",
                user_prompt="User",
            )

        assert "failed after 3 attempts" in exc_info.value.message
        assert "test-endpoint" in exc_info.value.message

    @patch("lakebase_accelerator.client.llm_client.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_on_transient_error_with_backoff(self, mock_sleep: AsyncMock, llm_client: LLMClient) -> None:
        """Retries on transient (5xx/timeout) errors with exponential backoff."""
        valid_response = json.dumps({"result": "success"})

        with patch.object(
            llm_client,
            "_invoke_endpoint",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("503 Service Unavailable"), valid_response],
        ):
            result = await llm_client.call(
                system_prompt="System",
                user_prompt="User",
            )

        assert result == {"result": "success"}
        # Backoff for first retry: 2^0 = 1 second
        mock_sleep.assert_called_once_with(1)

    @patch("lakebase_accelerator.client.llm_client.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_after_transient_retries_exhausted(self, mock_sleep: AsyncMock, llm_client: LLMClient) -> None:
        """Raises LLMClientError after transient error retries exhausted."""
        with (
            patch.object(
                llm_client,
                "_invoke_endpoint",
                new_callable=AsyncMock,
                side_effect=[
                    RuntimeError("timeout"),
                    RuntimeError("timeout"),
                    RuntimeError("timeout"),
                ],
            ),
            pytest.raises(LLMClientError) as exc_info,
        ):
            await llm_client.call(
                system_prompt="System",
                user_prompt="User",
            )

        assert "timeout" in exc_info.value.message


class TestLLMClientCheckReadiness:
    """Tests for LLMClient.check_readiness() method."""

    async def test_check_readiness_returns_true_for_ready_endpoint(
        self, llm_client: LLMClient, mock_workspace_client: MagicMock
    ) -> None:
        """Returns True when endpoint state is READY."""
        mock_endpoint = MagicMock()
        mock_endpoint.state.ready = "READY"
        mock_workspace_client.serving_endpoints.get.return_value = mock_endpoint

        result = await llm_client.check_readiness()

        assert result is True
        mock_workspace_client.serving_endpoints.get.assert_called_once_with("test-endpoint")

    async def test_check_readiness_caches_result(self, llm_client: LLMClient, mock_workspace_client: MagicMock) -> None:
        """Caches readiness result and does not call endpoint again."""
        mock_endpoint = MagicMock()
        mock_endpoint.state.ready = "READY"
        mock_workspace_client.serving_endpoints.get.return_value = mock_endpoint

        # First call
        result1 = await llm_client.check_readiness()
        # Second call should use cache
        result2 = await llm_client.check_readiness()

        assert result1 is True
        assert result2 is True
        # Only called once due to caching
        mock_workspace_client.serving_endpoints.get.assert_called_once()

    async def test_check_readiness_returns_false_on_not_ready(
        self, llm_client: LLMClient, mock_workspace_client: MagicMock
    ) -> None:
        """Returns False when endpoint state is not READY."""
        mock_endpoint = MagicMock()
        mock_endpoint.state.ready = "NOT_READY"
        mock_workspace_client.serving_endpoints.get.return_value = mock_endpoint

        result = await llm_client.check_readiness()

        assert result is False

    async def test_check_readiness_returns_false_on_error(
        self, llm_client: LLMClient, mock_workspace_client: MagicMock
    ) -> None:
        """Returns False when endpoint check raises an exception."""
        mock_workspace_client.serving_endpoints.get.side_effect = RuntimeError("Connection failed")

        result = await llm_client.check_readiness()

        assert result is False


class TestExtractJson:
    """Tests for LLMClient._extract_json() method."""

    def test_extract_json_plain(self, llm_client: LLMClient) -> None:
        """Parses plain JSON string correctly."""
        raw = '{"name": "test", "value": 123}'
        result = llm_client._extract_json(raw)
        assert result == {"name": "test", "value": 123}

    def test_extract_json_with_markdown_json_block(self, llm_client: LLMClient) -> None:
        """Strips ```json wrapper and parses content."""
        raw = '```json\n{"name": "test"}\n```'
        result = llm_client._extract_json(raw)
        assert result == {"name": "test"}

    def test_extract_json_with_markdown_block(self, llm_client: LLMClient) -> None:
        """Strips ``` wrapper without language tag and parses content."""
        raw = '```\n{"items": [1, 2, 3]}\n```'
        result = llm_client._extract_json(raw)
        assert result == {"items": [1, 2, 3]}

    def test_extract_json_with_whitespace(self, llm_client: LLMClient) -> None:
        """Handles leading/trailing whitespace around JSON."""
        raw = '  \n  {"key": "value"}  \n  '
        result = llm_client._extract_json(raw)
        assert result == {"key": "value"}

    def test_extract_json_raises_on_invalid(self, llm_client: LLMClient) -> None:
        """Raises JSONDecodeError on invalid JSON."""
        with pytest.raises(json.JSONDecodeError):
            llm_client._extract_json("not json at all")
