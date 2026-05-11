"""Unit tests for ModelConsumptionService."""

from unittest.mock import MagicMock, patch

import pytest

from lakebase_accelerator.services.model_consumption_service import ModelConsumptionService
from lakebase_accelerator.settings import ACCELERATOR_META_SCHEMA


@pytest.fixture
def mock_repo():
    """Create a mock LakebaseRepository."""
    return MagicMock()


@pytest.fixture
def service(mock_repo):
    """Create a ModelConsumptionService with mocked repository."""
    return ModelConsumptionService(lakebase_repo=mock_repo)


class TestLogConsumptionSuccess:
    """Tests for successful consumption logging."""

    def test_log_consumption_inserts_correct_data(self, service, mock_repo):
        """Test that log_consumption calls execute_query with correct SQL and params."""
        mock_repo.execute_query.return_value = []

        service.log_consumption(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            model_endpoint="databricks-claude-sonnet-4-5",
            model_name="claude-sonnet-4-5",
            call_type="intake",
            input_tokens=1500,
            output_tokens=800,
            total_tokens=2300,
            latency_ms=1234.5,
            status="success",
        )

        mock_repo.execute_query.assert_called_once()
        call_args = mock_repo.execute_query.call_args

        # Verify schema
        assert call_args[0][0] == ACCELERATOR_META_SCHEMA

        # Verify SQL contains INSERT into model_consumption
        query = call_args[0][1]
        assert "INSERT INTO accelerator_meta.model_consumption" in query
        assert "project_id" in query
        assert "model_endpoint" in query
        assert "model_name" in query
        assert "call_type" in query
        assert "input_tokens" in query
        assert "output_tokens" in query
        assert "total_tokens" in query
        assert "latency_ms" in query
        assert "status" in query

        # Verify params
        params = call_args[0][2]
        assert params == (
            "123e4567-e89b-12d3-a456-426614174000",
            "databricks-claude-sonnet-4-5",
            "claude-sonnet-4-5",
            "intake",
            1500,
            800,
            2300,
            1234.5,
            "success",
        )

    def test_log_consumption_with_zero_tokens(self, service, mock_repo):
        """Test logging with zero token values (edge case)."""
        mock_repo.execute_query.return_value = []

        service.log_consumption(
            project_id="00000000-0000-0000-0000-000000000000",
            model_endpoint="endpoint",
            model_name="model",
            call_type="test",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            latency_ms=0.0,
            status="success",
        )

        mock_repo.execute_query.assert_called_once()

    def test_log_consumption_with_large_token_counts(self, service, mock_repo):
        """Test logging with large token values."""
        mock_repo.execute_query.return_value = []

        service.log_consumption(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            model_endpoint="endpoint",
            model_name="model",
            call_type="generation",
            input_tokens=150000,
            output_tokens=8192,
            total_tokens=158192,
            latency_ms=45000.0,
            status="success",
        )

        mock_repo.execute_query.assert_called_once()
        params = mock_repo.execute_query.call_args[0][2]
        assert params[4] == 150000
        assert params[5] == 8192
        assert params[6] == 158192


class TestLogConsumptionFailure:
    """Tests for fire-and-forget error handling."""

    def test_log_consumption_does_not_raise_on_db_error(self, service, mock_repo):
        """Test that DB errors are swallowed (fire-and-forget)."""
        mock_repo.execute_query.side_effect = Exception("Connection refused")

        # Should NOT raise
        service.log_consumption(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            model_endpoint="endpoint",
            model_name="model",
            call_type="intake",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=500.0,
            status="success",
        )

    def test_log_consumption_does_not_raise_on_connection_pool_unavailable(self, service, mock_repo):
        """Test graceful handling when connection pool is unavailable."""
        mock_repo.execute_query.side_effect = RuntimeError(
            "Connection pool not initialized. Call set_connection_pool() during startup."
        )

        # Should NOT raise
        service.log_consumption(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            model_endpoint="endpoint",
            model_name="model",
            call_type="exploration",
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
            latency_ms=1000.0,
            status="error",
        )

    @patch("lakebase_accelerator.services.model_consumption_service.logger")
    def test_log_consumption_logs_warning_on_failure(self, mock_logger, mock_repo):
        """Test that failures are logged as warnings with context."""
        mock_repo.execute_query.side_effect = Exception("DB timeout")
        svc = ModelConsumptionService(lakebase_repo=mock_repo)

        svc.log_consumption(
            project_id="abc-123",
            model_endpoint="endpoint",
            model_name="model",
            call_type="intake",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=500.0,
            status="success",
        )

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "Failed to log model consumption to DB" in warning_msg
        assert "DB timeout" in warning_msg

    @patch("lakebase_accelerator.services.model_consumption_service.logger")
    def test_log_consumption_logs_info_on_success(self, mock_logger, mock_repo):
        """Test that successful logging emits an info log."""
        mock_repo.execute_query.return_value = []
        svc = ModelConsumptionService(lakebase_repo=mock_repo)

        svc.log_consumption(
            project_id="abc-123",
            model_endpoint="endpoint",
            model_name="model",
            call_type="intake",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=500.0,
            status="success",
        )

        mock_logger.info.assert_called_once()
        info_msg = mock_logger.info.call_args[0][0]
        assert "Model consumption logged" in info_msg

    def test_log_consumption_does_not_raise_on_psycopg2_error(self, service, mock_repo):
        """Test handling of psycopg2-specific errors."""
        import psycopg2

        mock_repo.execute_query.side_effect = psycopg2.OperationalError("server closed connection")

        # Should NOT raise
        service.log_consumption(
            project_id="123e4567-e89b-12d3-a456-426614174000",
            model_endpoint="endpoint",
            model_name="model",
            call_type="intake",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=500.0,
            status="success",
        )
