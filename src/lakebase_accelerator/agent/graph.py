"""Orchestrator Graph — LangGraph StateGraph connecting all pipeline nodes.

Sequential flow with single analysis checkpoint and self-healing deployment retry:

Greenfield: intake → (sufficient?) → analysis_review_checkpoint → design_model → schema
          → seed_data → backend → frontend → integration → deployment → END

Brownfield: brownfield_exploration → analysis_review_checkpoint → design_model → ...

Self-healing: deployment failure routes back to backend_dev for fix attempts.
"""

from typing import Literal

from langgraph.graph import END, START, StateGraph

from lakebase_accelerator.agent.nodes.backend_dev import backend_dev_node
from lakebase_accelerator.agent.nodes.brownfield_exploration import brownfield_exploration_node
from lakebase_accelerator.agent.nodes.checkpoint import analysis_review_checkpoint
from lakebase_accelerator.agent.nodes.data_model import data_model_node
from lakebase_accelerator.agent.nodes.deployment import deployment_node
from lakebase_accelerator.agent.nodes.frontend_dev import frontend_dev_node
from lakebase_accelerator.agent.nodes.intake import intake_node
from lakebase_accelerator.agent.nodes.integration import integration_node
from lakebase_accelerator.agent.nodes.schema_provisioning import schema_provisioning_node
from lakebase_accelerator.agent.nodes.seed_data import seed_data_node
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger


# ═══════════════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════


def _route_start(state: PipelineState) -> Literal["intake", "brownfield_exploration"]:
    """Route at START: greenfield goes to intake, brownfield goes to brownfield exploration."""
    if state.get("pipeline_type") == "brownfield":
        return "brownfield_exploration"
    return "intake"


def _route_after_intake(state: PipelineState) -> Literal["analysis_review_checkpoint", "__end__"]:
    """Route after intake: proceed to analysis review if sufficient, end if not."""
    if state.get("is_sufficient", True):
        return "analysis_review_checkpoint"
    return END


def _route_after_analysis_checkpoint(state: PipelineState) -> Literal["design_model", "brownfield_exploration", "intake", "__end__"]:
    """Route after analysis review checkpoint.

    - If approved (no corrections): proceed to design_model
    - If corrections provided: route back to the appropriate exploration/intake node
    - If error (timeout): end
    """
    error = state.get("error", "")
    if error:
        return END

    user_corrections = state.get("user_corrections", {})
    if user_corrections:
        # User provided corrections — re-run analysis with corrections
        pipeline_type = state.get("pipeline_type", "greenfield")
        if pipeline_type == "brownfield":
            return "brownfield_exploration"
        return "intake"

    return "design_model"


def _route_after_integration(state: PipelineState) -> Literal["deployment", "backend_dev", "__end__"]:
    """Route after integration: deploy if valid, retry backend if fixable, end if fatal."""
    error = state.get("error", "")
    if not error:
        return "deployment"

    if "Local import test failed" in error or "import" in error.lower():
        retry_count = state.get("deployment_retry_count", 0)
        if retry_count < 2:
            logger.info(
                f"Integration failed (import error), routing to backend_dev for fix (retry {retry_count})",
                extra={"step": "integration"},
            )
            return "backend_dev"

    return END


def _route_after_deployment(state: PipelineState) -> Literal["backend_dev", "__end__"]:
    """Route after deployment: retry via backend_dev if failed with retries left, else end."""
    if state.get("app_url") and not state.get("error"):
        return END

    if state.get("deployment_error_logs") and not state.get("error"):
        logger.info(
            f"Deployment failed, routing to backend_dev for self-healing "
            f"(retry {state.get('deployment_retry_count', 0)})",
            extra={"step": "deployment"},
        )
        return "backend_dev"

    return END


# ═══════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════


def build_pipeline_graph() -> StateGraph:
    """Build the pipeline graph with single analysis checkpoint for both flows.

    Flow:
    Greenfield:
      START → intake → (sufficient?) → analysis_review_checkpoint → design_model
            → schema → seed_data → backend_dev → frontend_dev → integration → deployment → END

    Brownfield:
      START → brownfield_exploration → analysis_review_checkpoint → design_model
            → schema → seed_data → backend_dev → frontend_dev → integration → deployment → END

    Single checkpoint shows a summary of what the agent found (entities, relationships,
    tech stack) and asks the user to confirm before proceeding to schema design.

    Self-healing:
      deployment failure → backend_dev → frontend_dev → integration → deployment (retry)
    """
    builder = StateGraph(PipelineState)

    # ─── Add all nodes ───────────────────────────────────────────────
    builder.add_node("intake", intake_node)
    builder.add_node("brownfield_exploration", brownfield_exploration_node)
    builder.add_node("analysis_review_checkpoint", analysis_review_checkpoint)
    builder.add_node("design_model", data_model_node)
    builder.add_node("schema_provisioning", schema_provisioning_node)
    builder.add_node("seed_data", seed_data_node)
    builder.add_node("backend_dev", backend_dev_node)
    builder.add_node("frontend_dev", frontend_dev_node)
    builder.add_node("integration", integration_node)
    builder.add_node("deployment", deployment_node)

    # ─── Entry routing ───────────────────────────────────────────────
    builder.add_conditional_edges(START, _route_start, ["intake", "brownfield_exploration"])

    # ─── Greenfield path ─────────────────────────────────────────────
    builder.add_conditional_edges("intake", _route_after_intake, ["analysis_review_checkpoint", END])

    # ─── Brownfield path ─────────────────────────────────────────────
    builder.add_edge("brownfield_exploration", "analysis_review_checkpoint")

    # ─── Single checkpoint → conditional routing ────────────────────────
    builder.add_conditional_edges(
        "analysis_review_checkpoint",
        _route_after_analysis_checkpoint,
        ["design_model", "brownfield_exploration", "intake", END],
    )

    # ─── Shared pipeline (both paths converge at design_model) ───────
    builder.add_edge("design_model", "schema_provisioning")
    builder.add_edge("schema_provisioning", "seed_data")
    builder.add_edge("seed_data", "backend_dev")
    builder.add_edge("backend_dev", "frontend_dev")
    builder.add_edge("frontend_dev", "integration")
    builder.add_conditional_edges("integration", _route_after_integration, ["deployment", "backend_dev", END])

    # ─── Self-healing ────────────────────────────────────────────────
    builder.add_conditional_edges("deployment", _route_after_deployment, ["backend_dev", END])

    logger.info("Pipeline graph built (single checkpoint + self-healing)")
    return builder.compile()


# ─── Singleton ───────────────────────────────────────────────────────

_compiled_graph = None


def get_pipeline_graph():
    """Get or create the compiled pipeline graph (singleton)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_pipeline_graph()
    return _compiled_graph
