"""Pipeline state — typed state that flows through the orchestrator graph.

Each node reads what it needs and writes its results back to state.
The orchestrator tracks progress and handles routing.
"""

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from typing_extensions import TypedDict


class PipelineState(TypedDict):
    """Full state for the greenfield/brownfield pipeline."""

    # ─── LangGraph Messages (agent reasoning history) ────────────────
    messages: Annotated[list[AnyMessage], add_messages]

    # ─── Input ───────────────────────────────────────────────────────
    prompt: str
    project_name: str
    pipeline_type: str  # "greenfield" or "brownfield"
    volume_paths: list[str]  # brownfield file paths

    # ─── After Intake Agent ──────────────────────────────────────────
    is_sufficient: bool  # True = proceed, False = ask clarification
    clarification_questions: list[str]
    entities: list[dict]  # extracted entities with attributes
    relationships: list[dict]  # entity relationships

    # ─── After Data Model Agent ──────────────────────────────────────
    data_model: dict  # full DDL-ready model (tables, columns, FKs, indexes, creation_order)

    # ─── After Schema Provisioning ───────────────────────────────────
    schema_name: str
    table_names: list[str]

    # ─── After Seed Data Agent ───────────────────────────────────────
    seed_row_counts: dict[str, int]

    # ─── After Backend Dev Agent ─────────────────────────────────────
    backend_files: dict[str, str]  # path → content

    # ─── After Frontend Dev Agent ────────────────────────────────────
    frontend_files: dict[str, str]  # path → content

    # ─── After Integration ───────────────────────────────────────────
    bundle_files: dict[str, str]  # all files merged + deployment config
    bundle_valid: bool

    # ─── After Deployment ────────────────────────────────────────────
    app_name: str
    app_url: str
    service_principal_id: str
    deployment_tested: bool

    # ─── Deployment Retry / Self-Healing ─────────────────────────────
    deployment_error_logs: str  # error logs from failed deployment
    deployment_retry_count: int  # number of retry attempts (max 2)
    deployment_fix_context: str  # context passed to backend_dev for fixing

    # ─── Progress ────────────────────────────────────────────────────
    current_step: str
    completed_steps: list[str]
    error: str
