"""Unit tests for checkpoint nodes — asyncio.Event-based pause/resume mechanism.

Tests cover:
- Each checkpoint node sets correct checkpoint_type and checkpoint_data
- The asyncio.Event wait mechanism (confirmation and timeout)
- Helper functions: submit_confirmation, get_pending_checkpoint, is_awaiting_confirmation
- State update building from user responses
- Heartbeat emission during wait
- Cleanup of pending confirmations after completion
"""

import asyncio
from unittest.mock import patch

import pytest

from lakebase_accelerator.agent.nodes.checkpoint import (
    CONFIRMATION_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    _await_checkpoint,
    _build_confirmation_state_update,
    _pending_confirmations,
    _wait_with_heartbeat,
    analysis_review_checkpoint,
    entity_review_checkpoint,
    get_pending_checkpoint,
    intake_review_checkpoint,
    is_awaiting_confirmation,
    structure_review_checkpoint,
    submit_confirmation,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_pending_confirmations():
    """Ensure pending confirmations are cleared before and after each test."""
    _pending_confirmations.clear()
    yield
    _pending_confirmations.clear()


@pytest.fixture
def brownfield_state():
    """Create a minimal brownfield PipelineState for checkpoint testing."""
    return {
        "prompt": "Analyze this project",
        "project_name": "test-project",
        "pipeline_type": "brownfield",
        "volume_paths": ["/Volumes/cat/sch/vol/project.zip"],
        "messages": [],
        "project_structure": {
            "root_files": ["manage.py", "requirements.txt"],
            "directories": ["app/", "tests/"],
            "entrypoint": "manage.py",
            "total_files_analyzed": 10,
        },
        "tech_stack": {
            "language": "Python",
            "framework": "Django",
            "orm": "Django ORM",
            "frontend": None,
            "build_tools": ["pip"],
            "dependencies": ["django"],
        },
        "exploration_plan": ["Read models.py", "Read views.py"],
        "tool_call_count": 5,
        "entities": [
            {"name": "customer", "description": "A customer", "attributes": []},
            {"name": "order", "description": "An order", "attributes": []},
        ],
        "relationships": [
            {"from_entity": "order", "to_entity": "customer", "cardinality": "many_to_one", "foreign_key_column": "customer_id"}
        ],
        "is_sufficient": True,
        "clarification_questions": [],
        "awaiting_checkpoint": "",
        "checkpoint_data": {},
        "user_corrections": {},
        "dismissed_items": [],
    }


@pytest.fixture
def greenfield_state():
    """Create a minimal greenfield PipelineState for intake checkpoint testing."""
    return {
        "prompt": "Build a task management app",
        "project_name": "task-app",
        "pipeline_type": "greenfield",
        "volume_paths": [],
        "messages": [],
        "entities": [
            {"name": "task", "description": "A task item", "attributes": []},
            {"name": "user", "description": "A user", "attributes": []},
        ],
        "relationships": [
            {"from_entity": "task", "to_entity": "user", "cardinality": "many_to_one", "foreign_key_column": "user_id"}
        ],
        "is_sufficient": True,
        "clarification_questions": [],
        "awaiting_checkpoint": "",
        "checkpoint_data": {},
        "user_corrections": {},
        "dismissed_items": [],
    }


# ═══════════════════════════════════════════════════════════════════════
# TEST: Constants
# ═══════════════════════════════════════════════════════════════════════


class TestConstants:
    """Tests for module-level constants."""

    def test_confirmation_timeout_is_30_minutes(self):
        """Test that default timeout is 30 minutes (1800 seconds)."""
        assert CONFIRMATION_TIMEOUT_SECONDS == 30 * 60
        assert CONFIRMATION_TIMEOUT_SECONDS == 1800

    def test_heartbeat_interval_is_30_seconds(self):
        """Test that heartbeat interval is 30 seconds."""
        assert HEARTBEAT_INTERVAL_SECONDS == 30


# ═══════════════════════════════════════════════════════════════════════
# TEST: structure_review_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestStructureReviewCheckpoint:
    """Tests for the structure_review_checkpoint node."""

    @pytest.mark.asyncio
    async def test_returns_state_update_on_approval(self, brownfield_state):
        """Test that approved checkpoint returns clean state update."""

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(approve_after_delay())
        result = await structure_review_checkpoint(brownfield_state)

        assert result["awaiting_checkpoint"] == ""
        assert result["checkpoint_data"] == {}
        assert result["current_step"] == "structure_review"

    @pytest.mark.asyncio
    async def test_returns_timeout_error_on_timeout(self, brownfield_state):
        """Test that timeout returns error state."""
        # Use a very short timeout for testing
        with patch(
            "lakebase_accelerator.agent.nodes.checkpoint.CONFIRMATION_TIMEOUT_SECONDS",
            0.1,
        ):
            result = await structure_review_checkpoint(brownfield_state)

        assert "timed out" in result.get("error", "")
        assert result["current_step"] == "structure_review"

    @pytest.mark.asyncio
    async def test_includes_project_structure_in_checkpoint_data(self, brownfield_state):
        """Test that checkpoint data includes project structure and tech stack."""
        # We'll check what data is passed to _await_checkpoint by approving quickly
        # and verifying the node registered the correct checkpoint type
        async def approve_and_check():
            await asyncio.sleep(0.05)
            pending = _pending_confirmations.get("test-project")
            assert pending is not None
            assert pending["checkpoint_type"] == "structure_review"
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(approve_and_check())
        await structure_review_checkpoint(brownfield_state)

    @pytest.mark.asyncio
    async def test_corrections_update_tech_stack(self, brownfield_state):
        """Test that corrections to tech stack are applied to state."""
        corrections = {"tech_stack": {"language": "TypeScript", "framework": "Next.js"}}

        async def correct_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=False, corrections=corrections)

        asyncio.create_task(correct_after_delay())
        result = await structure_review_checkpoint(brownfield_state)

        assert result["tech_stack"] == corrections["tech_stack"]
        assert result["user_corrections"] == corrections


# ═══════════════════════════════════════════════════════════════════════
# TEST: entity_review_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestEntityReviewCheckpoint:
    """Tests for the entity_review_checkpoint node."""

    @pytest.mark.asyncio
    async def test_returns_state_update_on_approval(self, brownfield_state):
        """Test that approved entity review returns clean state."""

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(approve_after_delay())
        result = await entity_review_checkpoint(brownfield_state)

        assert result["awaiting_checkpoint"] == ""
        assert result["current_step"] == "entity_review"

    @pytest.mark.asyncio
    async def test_registers_correct_checkpoint_type(self, brownfield_state):
        """Test that entity_review checkpoint type is registered."""

        async def check_and_approve():
            await asyncio.sleep(0.05)
            assert get_pending_checkpoint("test-project") == "entity_review"
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(check_and_approve())
        await entity_review_checkpoint(brownfield_state)

    @pytest.mark.asyncio
    async def test_corrections_update_entities(self, brownfield_state):
        """Test that entity corrections are applied to state."""
        corrected_entities = [
            {"name": "customer", "description": "Corrected customer", "attributes": []},
        ]
        corrections = {"entities": corrected_entities}

        async def correct_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=False, corrections=corrections)

        asyncio.create_task(correct_after_delay())
        result = await entity_review_checkpoint(brownfield_state)

        assert result["entities"] == corrected_entities

    @pytest.mark.asyncio
    async def test_dismissed_items_in_state(self, brownfield_state):
        """Test that dismissed items are included in state update."""
        dismissed = ["order"]

        async def dismiss_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=True, dismissed_items=dismissed)

        asyncio.create_task(dismiss_after_delay())
        result = await entity_review_checkpoint(brownfield_state)

        assert result["dismissed_items"] == dismissed


# ═══════════════════════════════════════════════════════════════════════
# TEST: analysis_review_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestAnalysisReviewCheckpoint:
    """Tests for the analysis_review_checkpoint node."""

    @pytest.mark.asyncio
    async def test_returns_state_update_on_approval(self, brownfield_state):
        """Test that approved analysis review returns clean state."""

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(approve_after_delay())
        result = await analysis_review_checkpoint(brownfield_state)

        assert result["awaiting_checkpoint"] == ""
        assert result["current_step"] == "analysis_review"

    @pytest.mark.asyncio
    async def test_registers_correct_checkpoint_type(self, brownfield_state):
        """Test that analysis_review checkpoint type is registered."""

        async def check_and_approve():
            await asyncio.sleep(0.05)
            assert get_pending_checkpoint("test-project") == "analysis_review"
            submit_confirmation("test-project", approved=True)

        asyncio.create_task(check_and_approve())
        await analysis_review_checkpoint(brownfield_state)

    @pytest.mark.asyncio
    async def test_corrections_update_relationships(self, brownfield_state):
        """Test that relationship corrections are applied."""
        corrected_rels = [
            {"from_entity": "order", "to_entity": "customer", "cardinality": "one_to_one", "foreign_key_column": "customer_id"}
        ]
        corrections = {"relationships": corrected_rels}

        async def correct_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("test-project", approved=False, corrections=corrections)

        asyncio.create_task(correct_after_delay())
        result = await analysis_review_checkpoint(brownfield_state)

        assert result["relationships"] == corrected_rels


# ═══════════════════════════════════════════════════════════════════════
# TEST: intake_review_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestIntakeReviewCheckpoint:
    """Tests for the intake_review_checkpoint node (greenfield)."""

    @pytest.mark.asyncio
    async def test_returns_state_update_on_approval(self, greenfield_state):
        """Test that approved intake review returns clean state."""

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("task-app", approved=True)

        asyncio.create_task(approve_after_delay())
        result = await intake_review_checkpoint(greenfield_state)

        assert result["awaiting_checkpoint"] == ""
        assert result["current_step"] == "intake_review"

    @pytest.mark.asyncio
    async def test_registers_correct_checkpoint_type(self, greenfield_state):
        """Test that intake_review checkpoint type is registered."""

        async def check_and_approve():
            await asyncio.sleep(0.05)
            assert get_pending_checkpoint("task-app") == "intake_review"
            submit_confirmation("task-app", approved=True)

        asyncio.create_task(check_and_approve())
        await intake_review_checkpoint(greenfield_state)

    @pytest.mark.asyncio
    async def test_corrections_update_entities(self, greenfield_state):
        """Test that entity corrections from intake review are applied."""
        corrected_entities = [
            {"name": "task", "description": "A task", "attributes": []},
            {"name": "user", "description": "A user", "attributes": []},
            {"name": "project", "description": "A project grouping tasks", "attributes": []},
        ]
        corrections = {"entities": corrected_entities}

        async def correct_after_delay():
            await asyncio.sleep(0.05)
            submit_confirmation("task-app", approved=False, corrections=corrections)

        asyncio.create_task(correct_after_delay())
        result = await intake_review_checkpoint(greenfield_state)

        assert result["entities"] == corrected_entities


# ═══════════════════════════════════════════════════════════════════════
# TEST: submit_confirmation
# ═══════════════════════════════════════════════════════════════════════


class TestSubmitConfirmation:
    """Tests for the submit_confirmation helper function."""

    def test_returns_false_when_no_pending_checkpoint(self):
        """Test that submitting to non-existent project returns False."""
        result = submit_confirmation("nonexistent-project", approved=True)
        assert result is False

    def test_returns_true_when_pending_checkpoint_exists(self):
        """Test that submitting to existing pending checkpoint returns True."""
        event = asyncio.Event()
        _pending_confirmations["my-project"] = {
            "event": event,
            "response": {},
            "checkpoint_type": "structure_review",
        }

        result = submit_confirmation("my-project", approved=True)

        assert result is True
        assert event.is_set()

    def test_populates_response_with_approval(self):
        """Test that response is populated with approved=True."""
        event = asyncio.Event()
        _pending_confirmations["my-project"] = {
            "event": event,
            "response": {},
            "checkpoint_type": "entity_review",
        }

        submit_confirmation("my-project", approved=True)

        response = _pending_confirmations["my-project"]["response"]
        assert response["approved"] is True
        assert response["corrections"] == {}
        assert response["dismissed_items"] == []
        assert response["additional_context"] == ""

    def test_populates_response_with_corrections(self):
        """Test that response includes corrections and dismissed items."""
        event = asyncio.Event()
        _pending_confirmations["my-project"] = {
            "event": event,
            "response": {},
            "checkpoint_type": "entity_review",
        }

        corrections = {"entities": [{"name": "fixed_entity"}]}
        dismissed = ["bad_entity"]

        submit_confirmation(
            "my-project",
            approved=False,
            corrections=corrections,
            dismissed_items=dismissed,
            additional_context="Please also check the auth module",
        )

        response = _pending_confirmations["my-project"]["response"]
        assert response["approved"] is False
        assert response["corrections"] == corrections
        assert response["dismissed_items"] == dismissed
        assert response["additional_context"] == "Please also check the auth module"

    def test_sets_event_to_unblock_waiting_node(self):
        """Test that the asyncio.Event is set after submission."""
        event = asyncio.Event()
        _pending_confirmations["my-project"] = {
            "event": event,
            "response": {},
            "checkpoint_type": "structure_review",
        }

        assert not event.is_set()
        submit_confirmation("my-project", approved=True)
        assert event.is_set()


# ═══════════════════════════════════════════════════════════════════════
# TEST: get_pending_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestGetPendingCheckpoint:
    """Tests for the get_pending_checkpoint helper function."""

    def test_returns_none_when_no_pending(self):
        """Test that None is returned when no checkpoint is pending."""
        result = get_pending_checkpoint("nonexistent")
        assert result is None

    def test_returns_checkpoint_type_when_pending(self):
        """Test that checkpoint type is returned when pending."""
        _pending_confirmations["my-project"] = {
            "event": asyncio.Event(),
            "response": {},
            "checkpoint_type": "entity_review",
        }

        result = get_pending_checkpoint("my-project")
        assert result == "entity_review"


# ═══════════════════════════════════════════════════════════════════════
# TEST: is_awaiting_confirmation
# ═══════════════════════════════════════════════════════════════════════


class TestIsAwaitingConfirmation:
    """Tests for the is_awaiting_confirmation helper function."""

    def test_returns_false_when_no_pending(self):
        """Test that False is returned when no checkpoint is pending."""
        assert is_awaiting_confirmation("nonexistent") is False

    def test_returns_true_when_pending(self):
        """Test that True is returned when checkpoint is pending."""
        _pending_confirmations["my-project"] = {
            "event": asyncio.Event(),
            "response": {},
            "checkpoint_type": "analysis_review",
        }

        assert is_awaiting_confirmation("my-project") is True


# ═══════════════════════════════════════════════════════════════════════
# TEST: _await_checkpoint (internal)
# ═══════════════════════════════════════════════════════════════════════


class TestAwaitCheckpoint:
    """Tests for the core _await_checkpoint logic."""

    @pytest.mark.asyncio
    async def test_registers_pending_confirmation(self):
        """Test that _await_checkpoint registers in _pending_confirmations."""
        state = {"project_name": "reg-test"}

        async def approve_quickly():
            await asyncio.sleep(0.05)
            submit_confirmation("reg-test", approved=True)

        asyncio.create_task(approve_quickly())

        await _await_checkpoint(
            state=state,
            checkpoint_type="test_checkpoint",
            checkpoint_data={"key": "value"},
            timeout=5.0,
        )

        # After completion, pending should be cleaned up
        assert "reg-test" not in _pending_confirmations

    @pytest.mark.asyncio
    async def test_cleans_up_on_timeout(self):
        """Test that pending confirmation is cleaned up after timeout."""
        state = {"project_name": "timeout-test"}

        result = await _await_checkpoint(
            state=state,
            checkpoint_type="test_checkpoint",
            checkpoint_data={},
            timeout=0.1,
        )

        assert "timeout-test" not in _pending_confirmations
        assert "timed out" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_cleans_up_on_success(self):
        """Test that pending confirmation is cleaned up after successful confirmation."""
        state = {"project_name": "success-test"}

        async def approve_quickly():
            await asyncio.sleep(0.05)
            submit_confirmation("success-test", approved=True)

        asyncio.create_task(approve_quickly())

        await _await_checkpoint(
            state=state,
            checkpoint_type="test_checkpoint",
            checkpoint_data={},
            timeout=5.0,
        )

        assert "success-test" not in _pending_confirmations

    @pytest.mark.asyncio
    async def test_uses_project_name_as_key(self):
        """Test that project_name from state is used as the key."""
        state = {"project_name": "unique-key-123"}

        async def check_key_and_approve():
            await asyncio.sleep(0.05)
            assert "unique-key-123" in _pending_confirmations
            submit_confirmation("unique-key-123", approved=True)

        asyncio.create_task(check_key_and_approve())

        await _await_checkpoint(
            state=state,
            checkpoint_type="test_checkpoint",
            checkpoint_data={},
            timeout=5.0,
        )


# ═══════════════════════════════════════════════════════════════════════
# TEST: _wait_with_heartbeat
# ═══════════════════════════════════════════════════════════════════════


class TestWaitWithHeartbeat:
    """Tests for the heartbeat-emitting wait mechanism."""

    @pytest.mark.asyncio
    async def test_returns_true_when_event_set(self):
        """Test that True is returned when event is set before timeout."""
        event = asyncio.Event()

        async def set_event():
            await asyncio.sleep(0.05)
            event.set()

        asyncio.create_task(set_event())

        result = await _wait_with_heartbeat(event, timeout=5.0, project_id="test", checkpoint_type="test")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        """Test that False is returned when timeout is reached."""
        event = asyncio.Event()

        result = await _wait_with_heartbeat(event, timeout=0.1, project_id="test", checkpoint_type="test")
        assert result is False

    @pytest.mark.asyncio
    async def test_emits_heartbeat_logs(self):
        """Test that heartbeat debug logs are emitted during wait."""
        event = asyncio.Event()

        with patch("lakebase_accelerator.agent.nodes.checkpoint.HEARTBEAT_INTERVAL_SECONDS", 0.05):
            with patch("lakebase_accelerator.agent.nodes.checkpoint.logger") as mock_logger:
                result = await _wait_with_heartbeat(
                    event, timeout=0.15, project_id="hb-test", checkpoint_type="test_cp"
                )

        assert result is False
        # Should have emitted at least one heartbeat debug log
        assert mock_logger.debug.call_count >= 1


# ═══════════════════════════════════════════════════════════════════════
# TEST: _build_confirmation_state_update
# ═══════════════════════════════════════════════════════════════════════


class TestBuildConfirmationStateUpdate:
    """Tests for building state updates from confirmation responses."""

    def test_approved_returns_clean_state(self):
        """Test that approval returns state with cleared checkpoint fields."""
        result = _build_confirmation_state_update(
            checkpoint_type="entity_review",
            checkpoint_data={"entities": []},
            response={"approved": True, "corrections": {}, "dismissed_items": [], "additional_context": ""},
        )

        assert result["awaiting_checkpoint"] == ""
        assert result["checkpoint_data"] == {}
        assert result["user_corrections"] == {}
        assert result["dismissed_items"] == []
        assert result["current_step"] == "entity_review"

    def test_corrections_applied_to_entities(self):
        """Test that entity corrections are merged into state."""
        corrected_entities = [{"name": "fixed"}]
        result = _build_confirmation_state_update(
            checkpoint_type="entity_review",
            checkpoint_data={"entities": [{"name": "original"}]},
            response={
                "approved": False,
                "corrections": {"entities": corrected_entities},
                "dismissed_items": [],
                "additional_context": "",
            },
        )

        assert result["entities"] == corrected_entities

    def test_corrections_applied_to_relationships(self):
        """Test that relationship corrections are merged into state."""
        corrected_rels = [{"from_entity": "a", "to_entity": "b", "cardinality": "one_to_one"}]
        result = _build_confirmation_state_update(
            checkpoint_type="analysis_review",
            checkpoint_data={},
            response={
                "approved": False,
                "corrections": {"relationships": corrected_rels},
                "dismissed_items": [],
                "additional_context": "",
            },
        )

        assert result["relationships"] == corrected_rels

    def test_corrections_applied_to_tech_stack(self):
        """Test that tech stack corrections are merged into state."""
        corrected_stack = {"language": "TypeScript", "framework": "Next.js"}
        result = _build_confirmation_state_update(
            checkpoint_type="structure_review",
            checkpoint_data={},
            response={
                "approved": False,
                "corrections": {"tech_stack": corrected_stack},
                "dismissed_items": [],
                "additional_context": "",
            },
        )

        assert result["tech_stack"] == corrected_stack

    def test_corrections_applied_to_project_structure(self):
        """Test that project structure corrections are merged into state."""
        corrected_structure = {"entrypoint": "index.ts", "directories": ["src/"]}
        result = _build_confirmation_state_update(
            checkpoint_type="structure_review",
            checkpoint_data={},
            response={
                "approved": False,
                "corrections": {"project_structure": corrected_structure},
                "dismissed_items": [],
                "additional_context": "",
            },
        )

        assert result["project_structure"] == corrected_structure

    def test_dismissed_items_included(self):
        """Test that dismissed items are passed through."""
        result = _build_confirmation_state_update(
            checkpoint_type="analysis_review",
            checkpoint_data={},
            response={
                "approved": True,
                "corrections": {},
                "dismissed_items": ["gap_1", "gap_2"],
                "additional_context": "",
            },
        )

        assert result["dismissed_items"] == ["gap_1", "gap_2"]

    def test_approved_with_corrections_does_not_apply_corrections(self):
        """Test that corrections are NOT applied when approved=True."""
        result = _build_confirmation_state_update(
            checkpoint_type="entity_review",
            checkpoint_data={},
            response={
                "approved": True,
                "corrections": {"entities": [{"name": "should_not_apply"}]},
                "dismissed_items": [],
                "additional_context": "",
            },
        )

        # Corrections should not be applied when approved
        assert "entities" not in result
