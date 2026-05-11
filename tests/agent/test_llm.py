"""Unit tests for the LLM instrumentation wrapper (invoke_with_logging)."""

from unittest.mock import MagicMock, patch

import pytest

from lakebase_accelerator.agent.llm import invoke_with_logging


@pytest.fixture
def mock_llm():
    """Create a mock ChatOpenAI instance."""
    llm = MagicMock()
    response = MagicMock()
    response.usage_metadata = {
        "input_tokens": 1500,
        "output_tokens": 800,
        "total_tokens": 2300,
    }
    response.content = "Generated response"
    llm.invoke.return_value = response
    return llm


@pytest.fixture
def mock_messages():
    """Create mock LangChain messages."""
    msg = MagicMock()
    msg.content = "Analyze this code"
    return [msg]


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.model_serving_endpoint = "databricks-claude-sonnet-4-5"
    return settings


class TestInvokeWithLoggingSuccess:
    """Tests for successful LLM invocation with logging."""

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_returns_llm_response(self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings):
        """Test that invoke_with_logging returns the LLM response unchanged."""
        mock_get_settings.return_value = mock_settings
        mock_get_service.return_value = MagicMock()

        result = invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        assert result == mock_llm.invoke.return_value

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_calls_llm_invoke_with_messages(
        self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings
    ):
        """Test that the LLM is invoked with the provided messages."""
        mock_get_settings.return_value = mock_settings
        mock_get_service.return_value = MagicMock()

        invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        mock_llm.invoke.assert_called_once_with(mock_messages)

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_logs_consumption_with_correct_token_usage(
        self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings
    ):
        """Test that token usage is extracted from response and logged."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="exploration",
        )

        mock_service.log_consumption.assert_called_once()
        call_kwargs = mock_service.log_consumption.call_args[1]

        assert call_kwargs["project_id"] == "123e4567-e89b-12d3-a456-426614174000"
        assert call_kwargs["model_endpoint"] == "databricks-claude-sonnet-4-5"
        assert call_kwargs["model_name"] == "databricks-claude-sonnet-4-5"
        assert call_kwargs["call_type"] == "exploration"
        assert call_kwargs["input_tokens"] == 1500
        assert call_kwargs["output_tokens"] == 800
        assert call_kwargs["total_tokens"] == 2300
        assert call_kwargs["status"] == "success"
        assert call_kwargs["latency_ms"] >= 0

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_measures_latency(self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings):
        """Test that latency is measured and passed to log_consumption."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        call_kwargs = mock_service.log_consumption.call_args[1]
        # Latency should be a positive float (in milliseconds)
        assert isinstance(call_kwargs["latency_ms"], float)
        assert call_kwargs["latency_ms"] >= 0


class TestInvokeWithLoggingMissingMetadata:
    """Tests for handling missing or malformed usage_metadata."""

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_handles_missing_usage_metadata(self, mock_get_settings, mock_get_service, mock_messages, mock_settings):
        """Test graceful handling when response has no usage_metadata."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        llm = MagicMock()
        response = MagicMock(spec=[])  # No attributes at all
        del response.usage_metadata  # Ensure getattr returns None
        response = MagicMock()
        response.usage_metadata = None
        llm.invoke.return_value = response

        result = invoke_with_logging(
            llm=llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        assert result == response
        call_kwargs = mock_service.log_consumption.call_args[1]
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0
        assert call_kwargs["total_tokens"] == 0

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_handles_empty_usage_metadata_dict(
        self, mock_get_settings, mock_get_service, mock_messages, mock_settings
    ):
        """Test graceful handling when usage_metadata is an empty dict."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        llm = MagicMock()
        response = MagicMock()
        response.usage_metadata = {}
        llm.invoke.return_value = response

        result = invoke_with_logging(
            llm=llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        assert result == response
        call_kwargs = mock_service.log_consumption.call_args[1]
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0
        assert call_kwargs["total_tokens"] == 0

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_handles_non_dict_usage_metadata(
        self, mock_get_settings, mock_get_service, mock_messages, mock_settings
    ):
        """Test graceful handling when usage_metadata is not a dict."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        llm = MagicMock()
        response = MagicMock()
        response.usage_metadata = "not a dict"
        llm.invoke.return_value = response

        result = invoke_with_logging(
            llm=llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        assert result == response
        call_kwargs = mock_service.log_consumption.call_args[1]
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0
        assert call_kwargs["total_tokens"] == 0


class TestInvokeWithLoggingFireAndForget:
    """Tests for fire-and-forget behavior — logging failures don't affect response."""

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_returns_response_when_logging_service_fails(
        self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings
    ):
        """Test that LLM response is returned even when consumption logging fails."""
        mock_get_settings.return_value = mock_settings
        mock_service = MagicMock()
        mock_service.log_consumption.side_effect = Exception("DB connection refused")
        mock_get_service.return_value = mock_service

        result = invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        # Response should still be returned
        assert result == mock_llm.invoke.return_value

    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_returns_response_when_service_unavailable(
        self, mock_get_settings, mock_get_service, mock_llm, mock_messages, mock_settings
    ):
        """Test that LLM response is returned when consumption service can't be created."""
        mock_get_settings.return_value = mock_settings
        mock_get_service.side_effect = RuntimeError(
            "Connection pool not initialized. Call set_connection_pool() during startup."
        )

        result = invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        # Response should still be returned
        assert result == mock_llm.invoke.return_value

    @patch("lakebase_accelerator.agent.llm.logger")
    @patch("lakebase_accelerator.agent.llm.get_model_consumption_service")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_logs_warning_when_consumption_logging_fails(
        self, mock_get_settings, mock_get_service, mock_logger, mock_llm, mock_messages, mock_settings
    ):
        """Test that a warning is logged when consumption logging fails."""
        mock_get_settings.return_value = mock_settings
        mock_get_service.side_effect = RuntimeError("Pool unavailable")

        invoke_with_logging(
            llm=mock_llm,
            messages=mock_messages,
            project_id="123e4567-e89b-12d3-a456-426614174000",
            call_type="intake",
        )

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "Failed to log model consumption" in warning_msg


class TestInvokeWithLoggingBackwardCompatibility:
    """Tests ensuring get_llm() remains unchanged."""

    @patch("lakebase_accelerator.agent.llm._get_workspace_token")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_get_llm_signature_unchanged(self, mock_get_settings, mock_get_token):
        """Test that get_llm() still works with its original signature."""
        from lakebase_accelerator.agent.llm import get_llm

        mock_settings = MagicMock()
        mock_settings.databricks_host = "https://test.cloud.databricks.com"
        mock_settings.model_serving_endpoint = "test-endpoint"
        mock_get_settings.return_value = mock_settings
        mock_get_token.return_value = "fake-token"

        llm = get_llm()
        assert llm is not None

    @patch("lakebase_accelerator.agent.llm._get_workspace_token")
    @patch("lakebase_accelerator.agent.llm.get_settings")
    def test_get_llm_accepts_max_tokens(self, mock_get_settings, mock_get_token):
        """Test that get_llm() still accepts max_tokens parameter."""
        from lakebase_accelerator.agent.llm import get_llm

        mock_settings = MagicMock()
        mock_settings.databricks_host = "https://test.cloud.databricks.com"
        mock_settings.model_serving_endpoint = "test-endpoint"
        mock_get_settings.return_value = mock_settings
        mock_get_token.return_value = "fake-token"

        llm = get_llm(max_tokens=8192)
        assert llm is not None
