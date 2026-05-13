"""In-memory event store for pipeline SSE events.

Decouples pipeline execution from HTTP connections. The pipeline runs as a
background asyncio.Task and pushes events to this store. Clients connect to
a separate SSE endpoint that reads from the store, supporting reconnection
if the Databricks Apps proxy drops the connection (~5 min hard timeout).

Events are stored per project_id and kept in memory for the lifetime of the
application process. Since Databricks Apps runs a single container, this is
sufficient for the accelerator's use case.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lakebase_accelerator.utils.logger import logger


@dataclass
class PipelineEvent:
    """A single SSE event emitted by the pipeline."""

    event_id: int
    event_type: str  # e.g., "step_completed", "heartbeat", "pipeline_complete"
    data: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class PipelineRun:
    """Tracks a single pipeline execution."""

    project_id: str
    events: list[PipelineEvent] = field(default_factory=list)
    is_complete: bool = False
    notify: asyncio.Event = field(default_factory=asyncio.Event)
    _event_counter: int = 0

    def push_event(self, event_type: str, data: dict[str, Any]) -> PipelineEvent:
        """Push a new event and notify waiting consumers."""
        self._event_counter += 1
        event = PipelineEvent(
            event_id=self._event_counter,
            event_type=event_type,
            data=data,
        )
        self.events.append(event)
        self.notify.set()  # Wake up any waiting SSE consumers
        self.notify.clear()  # Reset for next wait
        return event

    def mark_complete(self) -> None:
        """Mark the pipeline as complete so consumers know to stop."""
        self.is_complete = True
        self.notify.set()


# ─── Global Store ────────────────────────────────────────────────────

_pipeline_runs: dict[str, PipelineRun] = {}


def create_pipeline_run(project_id: str) -> PipelineRun:
    """Create a new pipeline run for tracking events."""
    run = PipelineRun(project_id=project_id)
    _pipeline_runs[project_id] = run
    logger.info(f"Pipeline run created: {project_id}")
    return run


def get_pipeline_run(project_id: str) -> PipelineRun | None:
    """Get an existing pipeline run by project_id."""
    return _pipeline_runs.get(project_id)


def cleanup_pipeline_run(project_id: str) -> None:
    """Remove a completed pipeline run from memory (optional cleanup)."""
    _pipeline_runs.pop(project_id, None)


def get_active_run_count() -> int:
    """Get the number of active (non-complete) pipeline runs."""
    return sum(1 for run in _pipeline_runs.values() if not run.is_complete)
