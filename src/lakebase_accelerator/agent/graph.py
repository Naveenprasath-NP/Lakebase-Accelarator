"""Orchestrator Graph — LangGraph StateGraph connecting all pipeline nodes.

Sequential flow with conditional routing and self-healing deployment retry:

Greenfield: intake → (sufficient?) → data_model → schema → seed_data → backend → frontend
                  → integration → (valid?) → deployment → (success?) → END

Brownfield: prototype_ingestion → (sufficient?) → data_model → ... (same as above)

Self-healing: deployment failure routes back to backend_dev for fix attempts.
"""

from typing import Literal

from langgraph.graph import END, START, StateGraph

from lakebase_accelerator.agent.nodes.backend_dev import backend_dev_node
from lakebase_accelerator.agent.nodes.data_model import data_model_node
from lakebase_accelerator.agent.nodes.deployment import deployment_node
from lakebase_accelerator.agent.nodes.frontend_dev import frontend_dev_node
from lakebase_accelerator.agent.nodes.intake import intake_node
from lakebase_accelerator.agent.nodes.integration import integration_node
from lakebase_accelerator.agent.nodes.prototype_analysis import prototype_analysis_node
from lakebase_accelerator.agent.nodes.prototype_ingestion import prototype_ingestion_node
from lakebase_accelerator.agent.nodes.schema_provisioning import schema_provisioning_node
from lakebase_accelerator.agent.nodes.seed_data import seed_data_node
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger


def _route_start(state: PipelineState) -> Literal["intake", "prototype_ingestion"]:
    """Route at START: greenfield goes to intake, brownfield goes to prototype ingestion."""
    if state.get("pipeline_type") == "brownfield":
        return "prototype_ingestion"
    return "intake"


def _route_after_intake(state: PipelineState) -> Literal["design_model", "__end__"]:
    """Route after intake/prototype_ingestion: proceed if sufficient, end if not."""
    if state.get("is_sufficient", True):
        return "design_model"
    return END


def _route_after_prototype_ingestion(state: PipelineState) -> Literal["prototype_analysis", "__end__"]:
    """Route after prototype_ingestion: proceed to analysis if sufficient, end if not."""
    if state.get("is_sufficient", True):
        return "prototype_analysis"
    return END


def _route_after_integration(state: PipelineState) -> Literal["deployment", "backend_dev", "__end__"]:
    """Route after integration: deploy if valid, retry backend if fixable, end if fatal.

    If the local import test fails, route back to backend_dev for self-healing
    instead of deploying broken code.
    """
    error = state.get("error", "")
    if not error:
        return "deployment"

    # If the error is from local import test, route to backend_dev for fixing
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
    """Route after deployment: retry via backend_dev if failed with retries left, else end.

    Self-healing flow:
    - If deployment succeeded (app_url set, no error) → END
    - If deployment failed but has error logs and retries left → route back to backend_dev
    - If deployment failed with max retries exceeded (error set) → END
    """
    # Success case
    if state.get("app_url") and not state.get("error"):
        return END

    # Failure with retries available — route back to backend_dev for self-healing
    if state.get("deployment_error_logs") and not state.get("error"):
        logger.info(
            f"Deployment failed, routing to backend_dev for self-healing "
            f"(retry {state.get('deployment_retry_count', 0)})",
            extra={"step": "deployment"},
        )
        return "backend_dev"

    # Max retries exceeded or unrecoverable error
    return END


def build_pipeline_graph() -> StateGraph:
    """Build the full orchestrator graph with brownfield support and self-healing.

    Flow:
    Greenfield:
      START → intake → (sufficient?) → design_model → schema → seed_data
            → backend_dev → frontend_dev → integration → deployment → END

    Brownfield:
      START → prototype_ingestion → (sufficient?) → prototype_analysis
            → design_model → schema → seed_data → backend_dev → frontend_dev
            → integration → deployment → END

    Self-healing:
      deployment failure → backend_dev → frontend_dev → integration → deployment (retry)
    """
    builder = StateGraph(PipelineState)

    # Add all nodes
    builder.add_node("intake", intake_node)
    builder.add_node("prototype_ingestion", prototype_ingestion_node)
    builder.add_node("prototype_analysis", prototype_analysis_node)
    builder.add_node("design_model", data_model_node)
    builder.add_node("schema_provisioning", schema_provisioning_node)
    builder.add_node("seed_data", seed_data_node)
    builder.add_node("backend_dev", backend_dev_node)
    builder.add_node("frontend_dev", frontend_dev_node)
    builder.add_node("integration", integration_node)
    builder.add_node("deployment", deployment_node)

    # ─── Entry routing ───────────────────────────────────────────────
    builder.add_conditional_edges(START, _route_start, ["intake", "prototype_ingestion"])

    # ─── Greenfield path ─────────────────────────────────────────────
    builder.add_conditional_edges("intake", _route_after_intake, ["design_model", END])

    # ─── Brownfield path (two-step ingestion) ────────────────────────
    builder.add_conditional_edges("prototype_ingestion", _route_after_prototype_ingestion, ["prototype_analysis", END])
    builder.add_edge("prototype_analysis", "design_model")

    # ─── Shared pipeline (both paths converge here) ──────────────────
    builder.add_edge("design_model", "schema_provisioning")
    builder.add_edge("schema_provisioning", "seed_data")
    builder.add_edge("seed_data", "backend_dev")
    builder.add_edge("backend_dev", "frontend_dev")
    builder.add_edge("frontend_dev", "integration")
    builder.add_conditional_edges("integration", _route_after_integration, ["deployment", "backend_dev", END])

    # Self-healing: deployment can route back to backend_dev or end
    builder.add_conditional_edges("deployment", _route_after_deployment, ["backend_dev", END])

    logger.info("Pipeline graph built (greenfield + brownfield multi-step + self-healing)")
    return builder.compile()


# ─── Singleton ───────────────────────────────────────────────────────

_compiled_graph = None


def get_pipeline_graph():
    """Get or create the compiled pipeline graph (singleton)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_pipeline_graph()
    return _compiled_graph
