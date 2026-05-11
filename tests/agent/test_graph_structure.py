"""Tests for the restructured pipeline graph with brownfield exploration and checkpoints.

Verifies:
- Brownfield flow: brownfield_exploration → structure_review_checkpoint → prototype_ingestion
  → entity_review_checkpoint → prototype_analysis → analysis_review_checkpoint → design_model
- Greenfield flow: intake → intake_review_checkpoint → design_model
- Checkpoint routing: corrections → re-run previous step, approved → next step
- Existing deployment self-healing and integration routing unchanged

**Validates: Requirements 2.1, 2.26, 2.29, 2.33, 2.44, 3.1, 3.3, 3.4**
"""

import pytest

from lakebase_accelerator.agent.graph import (
    _route_after_analysis_checkpoint,
    _route_after_deployment,
    _route_after_entity_checkpoint,
    _route_after_intake_checkpoint,
    _route_after_integration,
    _route_after_structure_checkpoint,
    _route_start,
    build_pipeline_graph,
)


# ═══════════════════════════════════════════════════════════════════════
# HELPER: Minimal state factory
# ═══════════════════════════════════════════════════════════════════════


def _make_state(**overrides) -> dict:
    """Create a minimal pipeline state dict with sensible defaults."""
    base = {
        "messages": [],
        "prompt": "test prompt",
        "project_name": "test-project",
        "pipeline_type": "greenfield",
        "volume_paths": [],
        "is_sufficient": True,
        "clarification_questions": [],
        "entities": [],
        "relationships": [],
        "prototype_context": "",
        "extracted_seed_data": {},
        "project_structure": {},
        "tech_stack": {},
        "exploration_plan": [],
        "tool_call_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "data_model": {},
        "schema_name": "",
        "table_names": [],
        "seed_row_counts": {},
        "backend_files": {},
        "frontend_files": {},
        "bundle_files": {},
        "bundle_valid": False,
        "app_name": "",
        "app_url": "",
        "service_principal_id": "",
        "deployment_tested": False,
        "deployment_error_logs": "",
        "deployment_retry_count": 0,
        "deployment_fix_context": "",
        "awaiting_checkpoint": "",
        "checkpoint_data": {},
        "user_corrections": {},
        "dismissed_items": [],
        "current_step": "",
        "completed_steps": [],
        "error": "",
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════
# TEST: _route_start
# ═══════════════════════════════════════════════════════════════════════


class TestRouteStart:
    """Tests for _route_start routing function."""

    def test_greenfield_routes_to_intake(self):
        """Greenfield pipeline routes to intake node."""
        state = _make_state(pipeline_type="greenfield")
        assert _route_start(state) == "intake"

    def test_brownfield_routes_to_brownfield_exploration(self):
        """Brownfield pipeline routes to brownfield_exploration node."""
        state = _make_state(pipeline_type="brownfield")
        assert _route_start(state) == "brownfield_exploration"

    def test_missing_pipeline_type_defaults_to_intake(self):
        """Missing pipeline_type defaults to greenfield (intake)."""
        state = _make_state()
        del state["pipeline_type"]
        assert _route_start(state) == "intake"


# ═══════════════════════════════════════════════════════════════════════
# TEST: _route_after_intake_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestRouteAfterIntakeCheckpoint:
    """Tests for _route_after_intake_checkpoint routing function."""

    def test_approved_routes_to_design_model(self):
        """When approved (no corrections, sufficient), routes to design_model."""
        state = _make_state(is_sufficient=True, user_corrections={}, error="")
        assert _route_after_intake_checkpoint(state) == "design_model"

    def test_corrections_routes_back_to_intake(self):
        """When user provides corrections, routes back to intake."""
        state = _make_state(
            is_sufficient=True,
            user_corrections={"entities": [{"name": "user"}]},
            error="",
        )
        assert _route_after_intake_checkpoint(state) == "intake"

    def test_error_routes_to_end(self):
        """When there's an error (e.g., timeout), routes to END."""
        state = _make_state(error="Checkpoint timed out")
        assert _route_after_intake_checkpoint(state) == "__end__"

    def test_not_sufficient_routes_to_end(self):
        """When not sufficient and no corrections, routes to END."""
        state = _make_state(is_sufficient=False, user_corrections={}, error="")
        assert _route_after_intake_checkpoint(state) == "__end__"


# ═══════════════════════════════════════════════════════════════════════
# TEST: _route_after_structure_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestRouteAfterStructureCheckpoint:
    """Tests for _route_after_structure_checkpoint routing function."""

    def test_approved_routes_to_prototype_ingestion(self):
        """When approved (no corrections), routes to prototype_ingestion (entity extraction)."""
        state = _make_state(user_corrections={}, error="")
        assert _route_after_structure_checkpoint(state) == "prototype_ingestion"

    def test_corrections_routes_back_to_brownfield_exploration(self):
        """When user provides corrections, routes back to brownfield_exploration."""
        state = _make_state(
            user_corrections={"tech_stack": {"framework": "Django"}},
            error="",
        )
        assert _route_after_structure_checkpoint(state) == "brownfield_exploration"

    def test_error_routes_to_end(self):
        """When there's an error, routes to END."""
        state = _make_state(error="Checkpoint timed out")
        assert _route_after_structure_checkpoint(state) == "__end__"


# ═══════════════════════════════════════════════════════════════════════
# TEST: _route_after_entity_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestRouteAfterEntityCheckpoint:
    """Tests for _route_after_entity_checkpoint routing function."""

    def test_approved_routes_to_prototype_analysis(self):
        """When approved (no corrections), routes to prototype_analysis (full analysis)."""
        state = _make_state(user_corrections={}, error="")
        assert _route_after_entity_checkpoint(state) == "prototype_analysis"

    def test_corrections_routes_back_to_prototype_ingestion(self):
        """When user provides corrections, routes back to prototype_ingestion."""
        state = _make_state(
            user_corrections={"entities": [{"name": "order", "attributes": []}]},
            error="",
        )
        assert _route_after_entity_checkpoint(state) == "prototype_ingestion"

    def test_error_routes_to_end(self):
        """When there's an error, routes to END."""
        state = _make_state(error="Timeout")
        assert _route_after_entity_checkpoint(state) == "__end__"


# ═══════════════════════════════════════════════════════════════════════
# TEST: _route_after_analysis_checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestRouteAfterAnalysisCheckpoint:
    """Tests for _route_after_analysis_checkpoint routing function."""

    def test_approved_routes_to_design_model(self):
        """When approved (no corrections), routes to design_model."""
        state = _make_state(user_corrections={}, error="")
        assert _route_after_analysis_checkpoint(state) == "design_model"

    def test_corrections_routes_back_to_prototype_analysis(self):
        """When user provides corrections, routes back to prototype_analysis."""
        state = _make_state(
            user_corrections={"relationships": [{"from": "order", "to": "user"}]},
            error="",
        )
        assert _route_after_analysis_checkpoint(state) == "prototype_analysis"

    def test_error_routes_to_end(self):
        """When there's an error, routes to END."""
        state = _make_state(error="Timeout")
        assert _route_after_analysis_checkpoint(state) == "__end__"


# ═══════════════════════════════════════════════════════════════════════
# TEST: Existing routing functions unchanged
# ═══════════════════════════════════════════════════════════════════════


class TestExistingRoutingUnchanged:
    """Verify _route_after_integration and _route_after_deployment remain unchanged."""

    def test_integration_success_routes_to_deployment(self):
        """No error after integration routes to deployment."""
        state = _make_state(error="")
        assert _route_after_integration(state) == "deployment"

    def test_integration_import_error_routes_to_backend_dev(self):
        """Import error with retries available routes to backend_dev."""
        state = _make_state(error="Local import test failed", deployment_retry_count=0)
        assert _route_after_integration(state) == "backend_dev"

    def test_integration_import_error_max_retries_routes_to_end(self):
        """Import error with max retries exceeded routes to END."""
        state = _make_state(error="Local import test failed", deployment_retry_count=2)
        assert _route_after_integration(state) == "__end__"

    def test_deployment_success_routes_to_end(self):
        """Successful deployment (app_url set, no error) routes to END."""
        state = _make_state(app_url="https://myapp.databricksapps.com", error="")
        assert _route_after_deployment(state) == "__end__"

    def test_deployment_failure_with_logs_routes_to_backend_dev(self):
        """Deployment failure with error logs routes to backend_dev for self-healing."""
        state = _make_state(
            app_url="",
            error="",
            deployment_error_logs="ModuleNotFoundError: No module named 'xyz'",
            deployment_retry_count=0,
        )
        assert _route_after_deployment(state) == "backend_dev"

    def test_deployment_max_retries_routes_to_end(self):
        """Deployment with error set (max retries) routes to END."""
        state = _make_state(
            app_url="",
            error="Max deployment retries exceeded",
            deployment_error_logs="some error",
        )
        assert _route_after_deployment(state) == "__end__"


# ═══════════════════════════════════════════════════════════════════════
# TEST: Graph structure (node and edge verification)
# ═══════════════════════════════════════════════════════════════════════


class TestGraphStructure:
    """Verify the compiled graph has the expected nodes and edges."""

    def test_graph_compiles_successfully(self):
        """The graph compiles without errors."""
        graph = build_pipeline_graph()
        assert graph is not None

    def test_graph_contains_all_expected_nodes(self):
        """Graph contains all required nodes for both flows."""
        graph = build_pipeline_graph()
        node_names = set(graph.nodes.keys())

        expected_nodes = {
            "intake",
            "intake_review_checkpoint",
            "brownfield_exploration",
            "structure_review_checkpoint",
            "prototype_ingestion",
            "entity_review_checkpoint",
            "prototype_analysis",
            "analysis_review_checkpoint",
            "design_model",
            "schema_provisioning",
            "seed_data",
            "backend_dev",
            "frontend_dev",
            "integration",
            "deployment",
            "__start__",  # LangGraph adds this
        }

        # Check all expected nodes are present (graph may have additional internal nodes)
        for node in expected_nodes:
            assert node in node_names, f"Expected node '{node}' not found in graph. Nodes: {node_names}"

    def test_graph_has_checkpoint_nodes(self):
        """Graph includes all four checkpoint nodes."""
        graph = build_pipeline_graph()
        node_names = set(graph.nodes.keys())

        checkpoint_nodes = {
            "intake_review_checkpoint",
            "structure_review_checkpoint",
            "entity_review_checkpoint",
            "analysis_review_checkpoint",
        }

        for node in checkpoint_nodes:
            assert node in node_names, f"Checkpoint node '{node}' not found in graph"

    def test_graph_has_brownfield_exploration_node(self):
        """Graph includes the brownfield_exploration node."""
        graph = build_pipeline_graph()
        assert "brownfield_exploration" in graph.nodes
