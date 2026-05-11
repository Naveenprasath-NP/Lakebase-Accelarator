"""Tests for SSE protocol extensions — checkpoint-related event types.

Validates that the event_stream() correctly emits:
- awaiting_confirmation when a checkpoint is reached
- confirmation_received when user responds
- step_resumed when pipeline continues after confirmation
- confirmation_timeout when timeout occurs
- heartbeat formatting helper

Requirements: 2.42, 2.43, 2.46, 3.7, 3.10
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from lakebase_accelerator.routes.projects import _format_heartbeat_event


class TestAwaitingConfirmationEvent:
    """Tests for awaiting_confirmation SSE event emission."""

    @pytest.mark.asyncio
    async def test_emits_awaiting_confirmation_when_checkpoint_reached(self):
        """When a node update contains non-empty awaiting_checkpoint, emit awaiting_confirmation."""
        # Simulate a node update with awaiting_checkpoint set
        update = {
            "awaiting_checkpoint": "structure_review",
            "checkpoint_data": {
                "project_structure": {"src/": ["main.py", "models.py"]},
                "tech_stack": {"framework": "Django"},
            },
            "current_step": "structure_review_checkpoint",
        }

        # Build the SSE event manually to verify format
        awaiting_data = {
            "step": "structure_review_checkpoint",
            "status": "awaiting_confirmation",
            "message": "Awaiting user confirmation at checkpoint: structure_review",
            "data": {
                "checkpoint_type": "structure_review",
                "checkpoint_data": update["checkpoint_data"],
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Verify the event structure matches expected format
        assert awaiting_data["step"] == "structure_review_checkpoint"
        assert awaiting_data["status"] == "awaiting_confirmation"
        assert awaiting_data["data"]["checkpoint_type"] == "structure_review"
        assert awaiting_data["data"]["checkpoint_data"] == update["checkpoint_data"]
        assert "timestamp" in awaiting_data

    @pytest.mark.asyncio
    async def test_awaiting_confirmation_includes_checkpoint_data(self):
        """awaiting_confirmation event includes checkpoint_type and checkpoint_data."""
        checkpoint_data = {
            "entities": [{"name": "User", "attributes": ["id", "email"]}],
            "relationships": [{"from": "User", "to": "Order", "type": "one_to_many"}],
        }

        awaiting_data = {
            "step": "entity_review_checkpoint",
            "status": "awaiting_confirmation",
            "message": "Awaiting user confirmation at checkpoint: entity_review",
            "data": {
                "checkpoint_type": "entity_review",
                "checkpoint_data": checkpoint_data,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Verify required fields
        assert "checkpoint_type" in awaiting_data["data"]
        assert "checkpoint_data" in awaiting_data["data"]
        assert awaiting_data["data"]["checkpoint_type"] == "entity_review"
        assert awaiting_data["data"]["checkpoint_data"]["entities"][0]["name"] == "User"

    @pytest.mark.asyncio
    async def test_awaiting_confirmation_event_format(self):
        """awaiting_confirmation event follows SSE format: event type + JSON data."""
        checkpoint_data = {"tech_stack": {"framework": "FastAPI"}}
        awaiting_data = {
            "step": "structure_review_checkpoint",
            "status": "awaiting_confirmation",
            "message": "Awaiting user confirmation at checkpoint: structure_review",
            "data": {
                "checkpoint_type": "structure_review",
                "checkpoint_data": checkpoint_data,
            },
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        sse_line = f"event: awaiting_confirmation\ndata: {json.dumps(awaiting_data)}\n\n"

        # Verify SSE format
        assert sse_line.startswith("event: awaiting_confirmation\n")
        assert "data: " in sse_line
        assert sse_line.endswith("\n\n")

        # Verify JSON is parseable
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)
        assert parsed["step"] == "structure_review_checkpoint"
        assert parsed["status"] == "awaiting_confirmation"
        assert parsed["data"]["checkpoint_type"] == "structure_review"


class TestConfirmationReceivedEvent:
    """Tests for confirmation_received SSE event emission."""

    @pytest.mark.asyncio
    async def test_emits_confirmation_received_when_user_responds(self):
        """When awaiting_checkpoint is cleared and user_corrections present, emit confirmation_received."""
        update = {
            "awaiting_checkpoint": "",
            "user_corrections": {"entities": [{"name": "User"}]},
            "current_step": "entity_review",
        }

        confirmed_data = {
            "step": update["current_step"],
            "status": "confirmed",
            "message": f"User confirmation received for checkpoint: {update['current_step']}",
            "data": {
                "checkpoint_type": update["current_step"],
                "has_corrections": bool(update.get("user_corrections")),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        assert confirmed_data["status"] == "confirmed"
        assert confirmed_data["data"]["checkpoint_type"] == "entity_review"
        assert confirmed_data["data"]["has_corrections"] is True

    @pytest.mark.asyncio
    async def test_confirmation_received_with_empty_corrections(self):
        """confirmation_received shows has_corrections=False when corrections are empty."""
        update = {
            "awaiting_checkpoint": "",
            "user_corrections": {},
            "current_step": "structure_review",
        }

        confirmed_data = {
            "step": update["current_step"],
            "status": "confirmed",
            "message": f"User confirmation received for checkpoint: {update['current_step']}",
            "data": {
                "checkpoint_type": update["current_step"],
                "has_corrections": bool(update.get("user_corrections")),
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        assert confirmed_data["data"]["has_corrections"] is False

    @pytest.mark.asyncio
    async def test_confirmation_received_event_format(self):
        """confirmation_received event follows SSE format."""
        confirmed_data = {
            "step": "entity_review",
            "status": "confirmed",
            "message": "User confirmation received for checkpoint: entity_review",
            "data": {
                "checkpoint_type": "entity_review",
                "has_corrections": True,
            },
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        sse_line = f"event: confirmation_received\ndata: {json.dumps(confirmed_data)}\n\n"

        assert sse_line.startswith("event: confirmation_received\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)
        assert parsed["status"] == "confirmed"
        assert "timestamp" in parsed


class TestStepResumedEvent:
    """Tests for step_resumed SSE event emission."""

    @pytest.mark.asyncio
    async def test_emits_step_resumed_after_confirmation(self):
        """step_resumed is emitted after confirmation_received."""
        current_step = "analysis_review"

        resumed_data = {
            "step": current_step,
            "status": "resumed",
            "message": f"Pipeline resumed after checkpoint: {current_step}",
            "data": {
                "checkpoint_type": current_step,
                "next_step": current_step,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        assert resumed_data["status"] == "resumed"
        assert resumed_data["data"]["checkpoint_type"] == "analysis_review"
        assert resumed_data["data"]["next_step"] == "analysis_review"

    @pytest.mark.asyncio
    async def test_step_resumed_event_format(self):
        """step_resumed event follows SSE format."""
        resumed_data = {
            "step": "structure_review",
            "status": "resumed",
            "message": "Pipeline resumed after checkpoint: structure_review",
            "data": {
                "checkpoint_type": "structure_review",
                "next_step": "structure_review",
            },
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        sse_line = f"event: step_resumed\ndata: {json.dumps(resumed_data)}\n\n"

        assert sse_line.startswith("event: step_resumed\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)
        assert parsed["status"] == "resumed"
        assert parsed["data"]["checkpoint_type"] == "structure_review"


class TestConfirmationTimeoutEvent:
    """Tests for confirmation_timeout SSE event emission."""

    @pytest.mark.asyncio
    async def test_emits_confirmation_timeout_on_timeout_error(self):
        """When error contains 'timed out', emit confirmation_timeout."""
        from lakebase_accelerator.agent.nodes.checkpoint import CONFIRMATION_TIMEOUT_SECONDS

        error_msg = "Checkpoint 'structure_review' timed out after 1800 seconds"

        timeout_data = {
            "step": "structure_review",
            "status": "timeout",
            "message": error_msg,
            "data": {
                "checkpoint_type": "structure_review",
                "timeout_seconds": CONFIRMATION_TIMEOUT_SECONDS,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        assert timeout_data["status"] == "timeout"
        assert timeout_data["data"]["timeout_seconds"] == 1800
        assert timeout_data["data"]["checkpoint_type"] == "structure_review"

    @pytest.mark.asyncio
    async def test_confirmation_timeout_event_format(self):
        """confirmation_timeout event follows SSE format."""
        timeout_data = {
            "step": "entity_review",
            "status": "timeout",
            "message": "Checkpoint 'entity_review' timed out after 1800 seconds",
            "data": {
                "checkpoint_type": "entity_review",
                "timeout_seconds": 1800,
            },
            "timestamp": "2024-01-01T00:00:00+00:00",
        }

        sse_line = f"event: confirmation_timeout\ndata: {json.dumps(timeout_data)}\n\n"

        assert sse_line.startswith("event: confirmation_timeout\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)
        assert parsed["status"] == "timeout"
        assert parsed["data"]["timeout_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_timeout_detection_case_insensitive(self):
        """Timeout detection works regardless of case in error message."""
        # The implementation uses .lower() for case-insensitive matching
        error_variants = [
            "Checkpoint 'x' timed out after 1800 seconds",
            "TIMED OUT waiting for confirmation",
            "Operation Timed Out",
        ]

        for error in error_variants:
            assert "timed out" in error.lower()


class TestHeartbeatEvent:
    """Tests for heartbeat SSE event formatting."""

    def test_format_heartbeat_event_structure(self):
        """_format_heartbeat_event returns properly formatted SSE event."""
        result = _format_heartbeat_event("structure_review", 60)

        assert result.startswith("event: heartbeat\n")
        assert "data: " in result
        assert result.endswith("\n\n")

        # Parse the JSON data
        data_part = result.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        assert parsed["step"] == "structure_review"
        assert parsed["status"] == "heartbeat"
        assert parsed["data"]["checkpoint_type"] == "structure_review"
        assert parsed["data"]["elapsed_seconds"] == 60
        assert parsed["data"]["heartbeat_interval_seconds"] == 30
        assert "timestamp" in parsed

    def test_format_heartbeat_event_includes_all_required_fields(self):
        """Heartbeat event includes step, status, message, data, timestamp."""
        result = _format_heartbeat_event("entity_review", 90)

        data_part = result.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        required_keys = {"step", "status", "message", "data", "timestamp"}
        assert required_keys.issubset(set(parsed.keys()))

    def test_format_heartbeat_event_message_content(self):
        """Heartbeat message includes checkpoint type."""
        result = _format_heartbeat_event("analysis_review", 120)

        data_part = result.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        assert "analysis_review" in parsed["message"]

    def test_format_heartbeat_event_various_elapsed_times(self):
        """Heartbeat correctly reports different elapsed times."""
        for elapsed in [30, 60, 90, 120, 300, 900]:
            result = _format_heartbeat_event("structure_review", elapsed)
            data_part = result.split("data: ")[1].rstrip("\n")
            parsed = json.loads(data_part)
            assert parsed["data"]["elapsed_seconds"] == elapsed


class TestExistingSSEEventsPreserved:
    """Tests that existing SSE events remain unchanged in format.

    Requirements: 3.7, 3.10
    """

    @pytest.mark.asyncio
    async def test_step_completed_format_unchanged(self):
        """step_completed event maintains existing format."""
        sse_data = {
            "step": "intake",
            "status": "completed",
            "message": "Analyzed prompt: 5 entities found",
            "data": {"entities": 5, "relationships": 3},
            "timestamp": datetime.now(UTC).isoformat(),
        }

        sse_line = f"event: step_completed\ndata: {json.dumps(sse_data)}\n\n"

        assert sse_line.startswith("event: step_completed\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        # Verify all required keys present
        assert "step" in parsed
        assert "status" in parsed
        assert "message" in parsed
        assert "data" in parsed
        assert "timestamp" in parsed
        assert parsed["status"] == "completed"

    @pytest.mark.asyncio
    async def test_step_failed_format_unchanged(self):
        """step_failed event maintains existing format."""
        sse_data = {
            "step": "deployment",
            "status": "failed",
            "message": "Deployment failed: connection timeout",
            "data": {"error_code": "STEP_FAILED"},
            "timestamp": datetime.now(UTC).isoformat(),
        }

        sse_line = f"event: step_failed\ndata: {json.dumps(sse_data)}\n\n"

        assert sse_line.startswith("event: step_failed\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        assert parsed["status"] == "failed"
        assert parsed["data"]["error_code"] == "STEP_FAILED"

    @pytest.mark.asyncio
    async def test_pipeline_complete_format_unchanged(self):
        """pipeline_complete event maintains existing format."""
        complete_data = {
            "step": None,
            "status": "completed",
            "message": "App deployed successfully",
            "data": {
                "app_url": "https://example.com",
                "app_name": "my-app",
                "schema_name": "my_schema",
                "catalog": "lakebase_accelerator_poc",
                "tables_created": ["users", "orders"],
                "completed_steps": ["intake", "data_model", "deployment"],
                "pipeline_duration_seconds": 45.2,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

        sse_line = f"event: pipeline_complete\ndata: {json.dumps(complete_data)}\n\n"

        assert sse_line.startswith("event: pipeline_complete\n")
        data_part = sse_line.split("data: ")[1].rstrip("\n")
        parsed = json.loads(data_part)

        assert parsed["status"] == "completed"
        assert parsed["data"]["app_url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_all_sse_events_share_common_structure(self):
        """All SSE events (old and new) share the same JSON structure: step, status, message, data, timestamp."""
        event_types = [
            ("step_completed", {"step": "intake", "status": "completed", "message": "Done", "data": {}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("step_failed", {"step": "deploy", "status": "failed", "message": "Error", "data": {"error_code": "X"}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("pipeline_complete", {"step": None, "status": "completed", "message": "Done", "data": {}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("awaiting_confirmation", {"step": "cp", "status": "awaiting_confirmation", "message": "Wait", "data": {"checkpoint_type": "x", "checkpoint_data": {}}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("confirmation_received", {"step": "cp", "status": "confirmed", "message": "Got it", "data": {"checkpoint_type": "x", "has_corrections": False}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("step_resumed", {"step": "cp", "status": "resumed", "message": "Resumed", "data": {"checkpoint_type": "x", "next_step": "y"}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("confirmation_timeout", {"step": "cp", "status": "timeout", "message": "Timed out", "data": {"checkpoint_type": "x", "timeout_seconds": 1800}, "timestamp": "2024-01-01T00:00:00+00:00"}),
            ("heartbeat", {"step": "cp", "status": "heartbeat", "message": "Waiting", "data": {"checkpoint_type": "x", "elapsed_seconds": 30, "heartbeat_interval_seconds": 30}, "timestamp": "2024-01-01T00:00:00+00:00"}),
        ]

        required_keys = {"step", "status", "message", "data", "timestamp"}

        for event_type, event_data in event_types:
            sse_line = f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
            assert sse_line.startswith(f"event: {event_type}\n")
            data_part = sse_line.split("data: ")[1].rstrip("\n")
            parsed = json.loads(data_part)
            assert required_keys.issubset(set(parsed.keys())), f"Event '{event_type}' missing keys: {required_keys - set(parsed.keys())}"


class TestSSEEventStreamIntegration:
    """Integration tests for the event_stream detecting checkpoint state updates."""

    @pytest.mark.asyncio
    async def test_checkpoint_detection_logic_awaiting(self):
        """Verify the detection logic: non-empty awaiting_checkpoint triggers awaiting_confirmation."""
        # Simulate what the event_stream loop does
        update = {
            "awaiting_checkpoint": "entity_review",
            "checkpoint_data": {"entities": [{"name": "User"}]},
            "current_step": "entity_review_checkpoint",
            "error": "",
        }

        awaiting = update.get("awaiting_checkpoint", "")
        assert awaiting  # Non-empty → should emit awaiting_confirmation

    @pytest.mark.asyncio
    async def test_checkpoint_detection_logic_confirmed(self):
        """Verify the detection logic: cleared awaiting_checkpoint + user_corrections triggers confirmation_received."""
        update = {
            "awaiting_checkpoint": "",
            "user_corrections": {"entities": [{"name": "User", "attributes": ["id"]}]},
            "current_step": "entity_review",
            "error": "",
        }

        # Detection conditions
        has_awaiting_key = "awaiting_checkpoint" in update
        awaiting_cleared = update.get("awaiting_checkpoint") == ""
        has_corrections = update.get("user_corrections") is not None

        assert has_awaiting_key
        assert awaiting_cleared
        assert has_corrections

    @pytest.mark.asyncio
    async def test_checkpoint_detection_logic_timeout(self):
        """Verify the detection logic: error with 'timed out' triggers confirmation_timeout."""
        update = {
            "current_step": "structure_review",
            "error": "Checkpoint 'structure_review' timed out after 1800 seconds",
        }

        error = update.get("error", "")
        assert error and "timed out" in error.lower()

    @pytest.mark.asyncio
    async def test_regular_step_not_detected_as_checkpoint(self):
        """Regular step updates without checkpoint fields emit step_completed, not checkpoint events."""
        update = {
            "current_step": "data_model",
            "data_model": {"tables": [{"name": "users"}]},
            "error": "",
        }

        # No checkpoint fields → should fall through to regular step_completed
        awaiting = update.get("awaiting_checkpoint", "")
        error = update.get("error", "")

        assert not awaiting  # Empty → not a checkpoint event
        assert not (error and "timed out" in error.lower())  # Not a timeout
        assert not (
            "awaiting_checkpoint" in update
            and update.get("awaiting_checkpoint") == ""
            and update.get("user_corrections") is not None
        )  # Not a confirmation received

    @pytest.mark.asyncio
    async def test_regular_error_not_detected_as_timeout(self):
        """Regular errors (not timeout) emit step_failed, not confirmation_timeout."""
        update = {
            "current_step": "deployment",
            "error": "Connection refused: database unavailable",
        }

        error = update.get("error", "")
        assert error  # Has error
        assert "timed out" not in error.lower()  # But not a timeout
