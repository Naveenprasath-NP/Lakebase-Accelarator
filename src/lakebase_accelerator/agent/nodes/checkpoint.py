"""Checkpoint nodes — pause pipeline execution and await user confirmation.

Each checkpoint node:
1. Sets `awaiting_checkpoint` and `checkpoint_data` in state
2. Creates an asyncio.Event keyed by project_id
3. Awaits the event with a configurable timeout (default 30 minutes)
4. When the event is set (by the confirm API), reads the response and returns state updates
5. If timeout, returns state with error indicating timeout

The confirm API endpoint (Task 8.1) calls `submit_confirmation()` to set the event
and unblock the waiting checkpoint node.

Requirements: 2.26, 2.27, 2.28, 2.29, 2.30, 2.31, 2.33, 2.34, 2.35, 2.36, 2.37, 2.42, 2.44, 2.45, 2.46, 2.47
"""

import asyncio
from typing import Any

from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger

# ─── Constants ────────────────────────────────────────────────────────

CONFIRMATION_TIMEOUT_SECONDS = 30 * 60
"""Default timeout for checkpoint confirmation (30 minutes)."""

HEARTBEAT_INTERVAL_SECONDS = 30
"""Interval between heartbeat emissions while awaiting confirmation."""

# ─── Module-Level Pending Confirmations ───────────────────────────────

_pending_confirmations: dict[str, dict] = {}
"""Pending confirmations keyed by project_id.

Each entry contains:
- 'event': asyncio.Event — set when user responds
- 'response': dict — populated by submit_confirmation with:
    - approved (bool)
    - corrections (dict)
    - dismissed_items (list[str])
    - additional_context (str)
- 'checkpoint_type': str — the current checkpoint type being awaited
"""


# ═══════════════════════════════════════════════════════════════════════
# CHECKPOINT NODES
# ═══════════════════════════════════════════════════════════════════════


async def structure_review_checkpoint(state: PipelineState) -> dict:
    """Pause after directory exploration for user review of project structure.

    Presents: project structure tree, tech stack, entrypoint, exploration plan.
    User can approve, correct tech stack, point to missed files, or skip areas.

    Requirements: 2.26, 2.27, 2.28
    """
    checkpoint_data = {
        "project_structure": state.get("project_structure", {}),
        "tech_stack": state.get("tech_stack", {}),
        "exploration_plan": state.get("exploration_plan", []),
        "tool_call_count": state.get("tool_call_count", 0),
    }

    return await _await_checkpoint(
        state=state,
        checkpoint_type="structure_review",
        checkpoint_data=checkpoint_data,
    )


async def entity_review_checkpoint(state: PipelineState) -> dict:
    """Pause after entity extraction for user review of entities and relationships.

    Presents: discovered entities with attributes, relationships with cardinalities.
    User can approve, add/remove entities, correct attributes, fix relationships.

    Requirements: 2.29, 2.30, 2.31
    """
    checkpoint_data = {
        "entities": state.get("entities", []),
        "relationships": state.get("relationships", []),
    }

    return await _await_checkpoint(
        state=state,
        checkpoint_type="entity_review",
        checkpoint_data=checkpoint_data,
    )


async def analysis_review_checkpoint(state: PipelineState) -> dict:
    """Pause after full analysis for user review of complete analysis summary.

    Presents: entities, relationships, API endpoints, UI structure, business logic,
    production gaps, seed data locations, and execution plan.
    User can approve, correct any section, add business rules, prioritize gaps.

    Requirements: 2.33, 2.34, 2.35, 2.36, 2.37
    """
    checkpoint_data = {
        "entities": state.get("entities", []),
        "relationships": state.get("relationships", []),
        "project_structure": state.get("project_structure", {}),
        "tech_stack": state.get("tech_stack", {}),
    }

    return await _await_checkpoint(
        state=state,
        checkpoint_type="analysis_review",
        checkpoint_data=checkpoint_data,
    )


async def intake_review_checkpoint(state: PipelineState) -> dict:
    """Pause after greenfield intake for user review of identified entities.

    Presents: entities, relationships, domain summary from prompt analysis.
    User can approve or correct the interpretation before data model design.

    Requirements: 2.44, 2.45
    """
    checkpoint_data = {
        "entities": state.get("entities", []),
        "relationships": state.get("relationships", []),
        "is_sufficient": state.get("is_sufficient", True),
        "clarification_questions": state.get("clarification_questions", []),
    }

    return await _await_checkpoint(
        state=state,
        checkpoint_type="intake_review",
        checkpoint_data=checkpoint_data,
    )


# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS (used by confirm API endpoint)
# ═══════════════════════════════════════════════════════════════════════


def submit_confirmation(
    project_id: str,
    approved: bool,
    corrections: dict | None = None,
    dismissed_items: list[str] | None = None,
    additional_context: str = "",
) -> bool:
    """Submit user confirmation for a pending checkpoint.

    Called by the confirm API endpoint to unblock the waiting checkpoint node.

    Args:
        project_id: The project ID whose checkpoint to confirm.
        approved: Whether the user approved the checkpoint.
        corrections: Optional corrections dict (section-specific).
        dismissed_items: Optional list of item IDs to dismiss.
        additional_context: Optional additional context from user.

    Returns:
        True if confirmation was submitted successfully, False if no pending checkpoint.
    """
    pending = _pending_confirmations.get(project_id)
    if pending is None:
        logger.warning(
            f"No pending checkpoint for project_id={project_id}",
            extra={"project_id": project_id},
        )
        return False

    # Populate the response
    pending["response"] = {
        "approved": approved,
        "corrections": corrections or {},
        "dismissed_items": dismissed_items or [],
        "additional_context": additional_context,
    }

    # Set the event to unblock the waiting node
    pending["event"].set()

    logger.info(
        f"Confirmation submitted for project_id={project_id}, approved={approved}",
        extra={"project_id": project_id, "checkpoint_type": pending.get("checkpoint_type")},
    )
    return True


def get_pending_checkpoint(project_id: str) -> str | None:
    """Get the current checkpoint type if the project is awaiting confirmation.

    Args:
        project_id: The project ID to check.

    Returns:
        The checkpoint type string if awaiting, None otherwise.
    """
    pending = _pending_confirmations.get(project_id)
    if pending is None:
        return None
    return pending.get("checkpoint_type")


def is_awaiting_confirmation(project_id: str) -> bool:
    """Check if a project is currently awaiting user confirmation.

    Args:
        project_id: The project ID to check.

    Returns:
        True if the project has a pending checkpoint, False otherwise.
    """
    return project_id in _pending_confirmations


# ═══════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


async def _await_checkpoint(
    state: PipelineState,
    checkpoint_type: str,
    checkpoint_data: dict[str, Any],
    timeout: float = CONFIRMATION_TIMEOUT_SECONDS,
) -> dict:
    """Core checkpoint logic: register pending confirmation and await user response.

    Steps:
    1. Register an asyncio.Event for this project_id
    2. Await the event with timeout
    3. On confirmation: return state updates based on user response
    4. On timeout: return state with timeout error

    Args:
        state: Current pipeline state.
        checkpoint_type: Type of checkpoint (structure_review, entity_review, etc.).
        checkpoint_data: Data to present to the user at this checkpoint.
        timeout: Timeout in seconds (default 30 minutes).

    Returns:
        Dict of state updates to merge into PipelineState.
    """
    project_id = state.get("project_name", "unknown")

    logger.info(
        f"Checkpoint '{checkpoint_type}' reached for project_id={project_id}",
        extra={"project_id": project_id, "checkpoint_type": checkpoint_type},
    )

    # Create the asyncio event for this checkpoint
    event = asyncio.Event()

    # Register the pending confirmation
    _pending_confirmations[project_id] = {
        "event": event,
        "response": {},
        "checkpoint_type": checkpoint_type,
    }

    try:
        # Await the event with timeout, emitting heartbeats while waiting
        confirmed = await _wait_with_heartbeat(event, timeout, project_id, checkpoint_type)

        if not confirmed:
            # Timeout occurred
            logger.warning(
                f"Checkpoint '{checkpoint_type}' timed out for project_id={project_id}",
                extra={"project_id": project_id, "checkpoint_type": checkpoint_type},
            )
            return {
                "awaiting_checkpoint": "",
                "checkpoint_data": {},
                "error": f"Checkpoint '{checkpoint_type}' timed out after {int(timeout)} seconds",
                "current_step": checkpoint_type,
            }

        # Confirmation received — read the response
        response = _pending_confirmations[project_id].get("response", {})

        logger.info(
            f"Checkpoint '{checkpoint_type}' confirmed for project_id={project_id}, "
            f"approved={response.get('approved')}",
            extra={"project_id": project_id, "checkpoint_type": checkpoint_type},
        )

        return _build_confirmation_state_update(
            checkpoint_type=checkpoint_type,
            checkpoint_data=checkpoint_data,
            response=response,
        )

    finally:
        # Clean up the pending confirmation
        _pending_confirmations.pop(project_id, None)


async def _wait_with_heartbeat(
    event: asyncio.Event,
    timeout: float,
    project_id: str,
    checkpoint_type: str,
) -> bool:
    """Wait for an event with periodic heartbeat logging.

    Emits a heartbeat log every HEARTBEAT_INTERVAL_SECONDS while waiting.
    The actual SSE heartbeat emission is handled by the SSE stream (Task 8.2).

    Args:
        event: The asyncio.Event to wait on.
        timeout: Total timeout in seconds.
        project_id: Project ID for logging.
        checkpoint_type: Checkpoint type for logging.

    Returns:
        True if event was set (confirmation received), False if timeout.
    """
    elapsed = 0.0

    while elapsed < timeout:
        wait_duration = min(HEARTBEAT_INTERVAL_SECONDS, timeout - elapsed)

        try:
            await asyncio.wait_for(event.wait(), timeout=wait_duration)
            return True  # Event was set — confirmation received
        except asyncio.TimeoutError:
            elapsed += wait_duration
            if elapsed < timeout:
                logger.debug(
                    f"Heartbeat: checkpoint '{checkpoint_type}' still awaiting confirmation "
                    f"(elapsed={int(elapsed)}s, project_id={project_id})",
                    extra={
                        "project_id": project_id,
                        "checkpoint_type": checkpoint_type,
                        "elapsed_seconds": int(elapsed),
                    },
                )

    return False  # Timeout reached


def _build_confirmation_state_update(
    checkpoint_type: str,
    checkpoint_data: dict[str, Any],
    response: dict,
) -> dict:
    """Build the state update dict from a user confirmation response.

    Args:
        checkpoint_type: The checkpoint type that was confirmed.
        checkpoint_data: The original data presented at the checkpoint.
        response: The user's response dict with approved, corrections, dismissed_items, additional_context.

    Returns:
        Dict of state updates to merge into PipelineState.
    """
    approved = response.get("approved", True)
    corrections = response.get("corrections", {})
    dismissed_items = response.get("dismissed_items", [])

    state_update: dict[str, Any] = {
        "awaiting_checkpoint": "",
        "checkpoint_data": {},
        "user_corrections": corrections,
        "dismissed_items": dismissed_items,
        "current_step": checkpoint_type,
    }

    # If user provided corrections, merge them into the relevant state fields
    if corrections and not approved:
        # Apply entity corrections if provided
        if "entities" in corrections:
            state_update["entities"] = corrections["entities"]
        if "relationships" in corrections:
            state_update["relationships"] = corrections["relationships"]
        if "tech_stack" in corrections:
            state_update["tech_stack"] = corrections["tech_stack"]
        if "project_structure" in corrections:
            state_update["project_structure"] = corrections["project_structure"]

    return state_update
