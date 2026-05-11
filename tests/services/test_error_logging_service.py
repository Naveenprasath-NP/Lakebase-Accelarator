"""Unit tests for ErrorLoggingService."""

from unittest.mock import MagicMock, patch

import pytest

from lakebase_accelerator.services.error_logging_service import ErrorLoggingService
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA


@pytest.fixture
def mock_repo():
    """Create a mock LakebaseRepository."""
    return MagicMock()


@pytest.fixture
def service(mock_repo):
    """Create an ErrorLoggingService with mocked repository."""
    return ErrorLoggingService(lakebase_repo=mock_repo)


class TestLogErrorSuccess:
    """Tests for successful error logging."""

    def test_log_error_inserts_correct_data(self, service, mock_repo):
        """Test that log_error calls execute_query with correct SQL and params."""
        mock_repo.execute_query.return_value = []

        service.log_error(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            error_code="LLM_PARSE_ERROR",
            severity="high",
            source_component="intake_node",
            source_step="intake",
            error_message="Failed to parse LLM response as JSON",
            stack_trace="Traceback (most recent call last):\n  File ...",
            error_context="Response was truncated at 4096 tokens",
            is_retryable=True,
        )

        mock_repo.execute_query.assert_called_once()
        call_args = mock_repo.execute_query.call_args

        # Verify schema
        assert call_args[0][0] == ACCELERATOR_META_SCHEMA

        # Verify SQL contains INSERT into error_logs
        query = call_args[0][1]
        assert "INSERT INTO accelerator_meta.error_logs" in query
        assert "project_id" in query
        assert "error_code" in query
        assert "severity" in query
        assert "source_component" in query
        assert "source_step" in query
        assert "error_message" in query
        assert "stack_trace" in query
        assert "error_context" in query
        assert "is_retryable" in query

        # Verify params
        params = call_args[0][2]
        assert params == (
            "123e4567-e89b-12d3-a456-426614174000",
            "LLM_PARSE_ERROR",
            "high",
            "intake_node",
            "intake",
            "Failed to parse LLM response as JSON",
            "Traceback (most recent call last):\n  File ...",
            "Response was truncated at 4096 tokens",
            True,
        )

    def test_log_error_with_optional_fields_none(self, service, mock_repo):
        """Test logging with optional fields set to None (defaults)."""
        mock_repo.execute_query.return_value = []

        service.log_error(
            project_id="00000000-0000-0000-0000-000000000000",
            error_code="DB_TIMEOUT",
            severity="medium",
            source_component="schema_provisioning",
            source_step="schema",
            error_message="Database connection timed out",
        )

        mock_repo.execute_query.assert_called_once()
        params = mock_repo.execute_query.call_args[0][2]
        # stack_trace, error_context should be None, is_retryable should be False
        assert params[6] is None  # stack_trace
        assert params[7] is None  # error_context
        assert params[8] is False  # is_retryable

    def test_log_error_with_long_error_message(self, service, mock_repo):
        """Test logging with a very long error message."""
        mock_repo.execute_query.return_value = []
        long_message = "x" * 10000

        service.log_error(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            error_code="UNEXPECTED_ERROR",
            severity="critical",
            source_component="deployment_node",
            source_step="deployment",
            error_message=long_message,
            stack_trace="long trace...",
            error_context="context",
            is_retryable=False,
        )

        mock_repo.execute_query.assert_called_once()
        params = mock_repo.execute_query.call_args[0][2]
        assert params[5] == long_message


class TestLogErrorFailure:
    """Tests for fire-and-forget error handling."""

    def test_log_error_does_not_raise_on_db_error(self, service, mock_repo):
        """Test that DB errors are swallowed (fire-and-forget)."""
        mock_repo.execute_query.side_effect = Exception("Connection refused")

        # Should NOT raise
        service.log_error(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            error_code="LLM_PARSE_ERROR",
            severity="high",
            source_component="intake_node",
            source_step="intake",
            error_message="Some error occurred",
        )

    def test_log_error_does_not_raise_on_connection_pool_unavailable(self, service, mock_repo):
        """Test graceful handling when connection pool is unavailable."""
        mock_repo.execute_query.side_effect = RuntimeError(
            "Connection pool not initialized. Call set_connection_pool() during startup."
        )

        # Should NOT raise
        service.log_error(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            error_code="POOL_UNAVAILABLE",
            severity="low",
            source_component="error_logging_service",
            source_step="logging",
            error_message="Pool not available",
        )

    @patch("lakebase_accelerator.services.error_logging_service.logger")
    def test_log_error_logs_warning_on_failure(self, mock_logger, mock_repo):
        """Test that failures are logged as warnings with context."""
        mock_repo.execute_query.side_effect = Exception("DB timeout")
        svc = ErrorLoggingService(lakebase_repo=mock_repo)

        svc.log_error(
            project_id="abc-123",
            error_code="TEST_ERROR",
            severity="medium",
            source_component="test_component",
            source_step="test_step",
            error_message="Test error message",
        )

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "Failed to log error to DB" in warning_msg
        assert "DB timeout" in warning_msg

    @patch("lakebase_accelerator.services.error_logging_service.logger")
    def test_log_error_logs_info_on_success(self, mock_logger, mock_repo):
        """Test that successful logging emits an info log."""
        mock_repo.execute_query.return_value = []
        svc = ErrorLoggingService(lakebase_repo=mock_repo)

        svc.log_error(
            project_id="abc-123",
            error_code="LLM_PARSE_ERROR",
            severity="high",
            source_component="intake_node",
            source_step="intake",
            error_message="Parse failed",
        )

        mock_logger.info.assert_called_once()
        info_msg = mock_logger.info.call_args[0][0]
        assert "Error logged to DB" in info_msg

    def test_log_error_does_not_raise_on_psycopg2_error(self, service, mock_repo):
        """Test handling of psycopg2-specific errors."""
        import psycopg2

        mock_repo.execute_query.side_effect = psycopg2.OperationalError("server closed connection")

        # Should NOT raise
        service.log_error(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            error_code="DB_ERROR",
            severity="high",
            source_component="schema_provisioning",
            source_step="schema",
            error_message="Schema creation failed",
            stack_trace="traceback...",
            error_context="context...",
            is_retryable=True,
        )
