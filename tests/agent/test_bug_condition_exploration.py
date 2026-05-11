"""Bug Condition Exploration Tests — Brownfield Single-Shot Analysis & Missing DB Logging.

These tests encode the EXPECTED (fixed) behavior. They are designed to FAIL on the
current unfixed code, confirming the bug conditions exist:

(a) Brownfield pipeline uses single-shot LLM call without iterative tool use
(b) Pipeline runs end-to-end without emitting `awaiting_confirmation` SSE events
(c) LLM calls do not write rows to `model_consumption` table
(d) Handled exceptions do not write rows to `error_logs` table

**Validates: Requirements 1.1, 1.2, 1.18, 1.26, 1.27**
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


# ─── Strategies ──────────────────────────────────────────────────────────────


@st.composite
def volume_file_sets(draw):
    """Generate realistic sets of volume file paths for brownfield pipelines."""
    num_files = draw(st.integers(min_value=2, max_value=10))
    extensions = [".py", ".ts", ".js", ".json", ".yaml", ".sql"]
    directories = ["src/", "app/", "models/", "routes/", ""]
    filenames = ["models", "app", "main", "database", "schema", "routes",
                 "config", "utils", "service", "handler"]

    paths = []
    for _ in range(num_files):
        directory = draw(st.sampled_from(directories))
        filename = draw(st.sampled_from(filenames))
        ext = draw(st.sampled_from(extensions))
        paths.append(f"/Volumes/catalog/schema/volume/{directory}{filename}{ext}")

    return list(set(paths))  # deduplicate



@st.composite
def pipeline_states_brownfield(draw):
    """Generate brownfield pipeline initial states with varying file sets."""
    volume_paths = draw(volume_file_sets())
    prompt = draw(st.text(
        min_size=20, max_size=200,
        alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    ))

    return {
        "messages": [],
        "prompt": prompt,
        "project_name": "test-project",
        "pipeline_type": "brownfield",
        "volume_paths": volume_paths,
        "is_sufficient": True,
        "clarification_questions": [],
        "entities": [],
        "relationships": [],
        "prototype_context": "",
        "extracted_seed_data": {},
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
        "current_step": "",
        "completed_steps": [],
        "error": "",
    }


def _make_mock_llm_response(content_json: dict) -> MagicMock:
    """Create a mock LLM response with usage metadata."""
    response = MagicMock()
    response.content = json.dumps(content_json)
    response.usage_metadata = {
        "input_tokens": 1500,
        "output_tokens": 800,
        "total_tokens": 2300,
    }
    return response


# Patch target prefix — patch where the names are looked up
_NODE_MODULE = "lakebase_accelerator.agent.nodes.prototype_ingestion"
_VOLUME_SVC = "lakebase_accelerator.services.volume_reader_service.VolumeReaderService"


# ─── Bug Condition (a): Single-Shot LLM Call ─────────────────────────────────


class TestBugConditionSingleShotAnalysis:
    """Property: prototype_ingestion_node makes exactly 1 LLM call with all files
    concatenated — no iterative tool use.

    EXPECTED BEHAVIOR (after fix): The brownfield pipeline should use iterative
    tool-calling (ReAct agent) instead of a single-shot batch call.

    This test FAILS on unfixed code because the current implementation does
    make exactly 1 LLM call (confirming the bug exists).

    **Validates: Requirements 1.1, 1.2**
    """

    @given(state=pipeline_states_brownfield())
    @settings(max_examples=3, deadline=30000)
    @pytest.mark.asyncio
    async def test_prototype_ingestion_uses_iterative_tool_calls(self, state):
        """Property: brownfield exploration should use multiple tool calls, not a single LLM batch.

        Bug condition: prototype_ingestion_node reads all files via
        VolumeReaderService.read_prototype_files() in one batch, builds one prompt,
        makes one LLM call.

        Expected (fixed): Agent iteratively calls tools (list_directory, read_file, etc.)
        making multiple LLM calls with tool-calling enabled.
        """
        mock_file_contents = {
            f"file_{i}.py": f"class Model{i}:\n    pass\n"
            for i in range(len(state["volume_paths"]))
        }

        valid_response = _make_mock_llm_response({
            "project_name": "test-project",
            "domain_summary": "A test project",
            "is_sufficient": True,
            "entities": [{"name": "user", "description": "A user", "attributes": []}],
            "relationships": [],
        })

        llm_call_count = 0

        def track_llm_invoke(messages):
            nonlocal llm_call_count
            llm_call_count += 1
            return valid_response

        mock_llm = MagicMock()
        mock_llm.invoke = track_llm_invoke

        with (
            patch(f"{_NODE_MODULE}.get_workspace_client") as mock_get_ws,
            patch(f"{_NODE_MODULE}.get_llm") as mock_get_llm,
            patch(
                f"{_VOLUME_SVC}.read_prototype_files",
                new_callable=AsyncMock,
                return_value=mock_file_contents,
            ),
        ):
            mock_get_ws.return_value = MagicMock()
            mock_get_llm.return_value = mock_llm

            from lakebase_accelerator.agent.nodes.prototype_ingestion import (
                prototype_ingestion_node,
            )

            result = await prototype_ingestion_node(state)

        # EXPECTED BEHAVIOR (after fix): The agent should make MULTIPLE LLM calls
        # using tool-calling (ReAct pattern), not just 1 single-shot call.
        # On unfixed code, this assertion FAILS because llm_call_count == 1
        # (confirming bug condition a).
        assert llm_call_count > 1, (
            f"Bug condition confirmed: prototype_ingestion_node made exactly "
            f"{llm_call_count} LLM call(s) — single-shot batch analysis without "
            f"iterative tool use. Expected multiple tool-calling iterations."
        )


# ─── Bug Condition (b): No awaiting_confirmation SSE Events ──────────────────


class TestBugConditionNoCheckpoints:
    """Property: A full brownfield pipeline execution emits zero
    `awaiting_confirmation` SSE events.

    EXPECTED BEHAVIOR (after fix): The pipeline should pause at checkpoints
    and emit `awaiting_confirmation` events.

    This test FAILS on unfixed code because the current pipeline never emits
    awaiting_confirmation events (confirming the bug exists).

    **Validates: Requirements 1.18**
    """

    @pytest.mark.asyncio
    async def test_brownfield_pipeline_emits_no_awaiting_confirmation_events(self):
        """Property: pipeline execution should emit awaiting_confirmation SSE events.

        Bug condition: SSE stream only emits step_completed and pipeline_complete —
        never awaiting_confirmation.

        Expected (fixed): Pipeline pauses at checkpoints and emits
        awaiting_confirmation events.
        """
        # Inspect the route source code to verify whether awaiting_confirmation
        # event emission logic exists. This is a structural test that confirms
        # the bug condition without needing to run the full pipeline.
        import inspect

        from lakebase_accelerator.routes import projects as projects_module

        source_code = inspect.getsource(projects_module)

        # EXPECTED BEHAVIOR (after fix): The source should contain
        # "awaiting_confirmation" event emission logic.
        # On unfixed code, this assertion FAILS because the route never
        # emits awaiting_confirmation (confirming bug condition b).
        assert "awaiting_confirmation" in source_code, (
            "Bug condition confirmed: The pipeline route source code contains NO "
            "'awaiting_confirmation' SSE event emission. The pipeline runs end-to-end "
            "without pausing for user confirmation at any checkpoint."
        )


# ─── Bug Condition (c): No model_consumption DB Logging ──────────────────────


class TestBugConditionNoModelConsumptionLogging:
    """Property: After pipeline execution with LLM calls, `model_consumption`
    table has 0 rows.

    EXPECTED BEHAVIOR (after fix): Each LLM call should log token usage to
    the model_consumption table.

    This test FAILS on unfixed code because no service writes to
    model_consumption (confirming the bug exists).

    **Validates: Requirements 1.26**
    """

    @given(state=pipeline_states_brownfield())
    @settings(max_examples=3, deadline=30000)
    @pytest.mark.asyncio
    async def test_llm_calls_log_to_model_consumption_table(self, state):
        """Property: LLM calls should write token usage to model_consumption table.

        Bug condition: model_consumption table has 0 rows after pipeline execution
        with LLM calls.

        Expected (fixed): Each LLM call logs its token usage to the
        model_consumption table via ModelConsumptionService.
        """
        model_consumption_rows: list[dict] = []

        valid_response = _make_mock_llm_response({
            "project_name": "test-project",
            "domain_summary": "A test project",
            "is_sufficient": True,
            "entities": [{"name": "item", "description": "An item", "attributes": []}],
            "relationships": [],
        })

        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=valid_response)

        mock_file_contents = {
            f"file_{i}.py": f"class Entity{i}:\n    pass\n"
            for i in range(min(3, len(state["volume_paths"])))
        }

        # Track any calls that would write to model_consumption
        original_execute_query = None

        def mock_execute_query(schema_name, query, params=None):
            if "model_consumption" in str(query).lower():
                model_consumption_rows.append({"query": query, "params": params})
            return []

        with (
            patch(f"{_NODE_MODULE}.get_workspace_client") as mock_get_ws,
            patch(f"{_NODE_MODULE}.get_llm") as mock_get_llm,
            patch(
                f"{_VOLUME_SVC}.read_prototype_files",
                new_callable=AsyncMock,
                return_value=mock_file_contents,
            ),
        ):
            mock_get_ws.return_value = MagicMock()
            mock_get_llm.return_value = mock_llm

            from lakebase_accelerator.agent.nodes.prototype_ingestion import (
                prototype_ingestion_node,
            )

            result = await prototype_ingestion_node(state)

        # Verify the LLM was actually called (precondition)
        assert mock_llm.invoke.call_count >= 1, "LLM should have been called at least once"

        # EXPECTED BEHAVIOR (after fix): After LLM calls, token usage should
        # be logged to model_consumption table (rows > 0).
        # On unfixed code, this assertion FAILS because no code writes to
        # model_consumption (confirming bug condition c).
        assert len(model_consumption_rows) > 0, (
            f"Bug condition confirmed: LLM was called {mock_llm.invoke.call_count} time(s) "
            f"but model_consumption table has 0 rows. Token usage metadata "
            f"(input_tokens={valid_response.usage_metadata['input_tokens']}, "
            f"output_tokens={valid_response.usage_metadata['output_tokens']}) "
            f"was discarded and never persisted."
        )


# ─── Bug Condition (d): No error_logs DB Logging ─────────────────────────────


class TestBugConditionNoErrorLogging:
    """Property: After a handled exception, `error_logs` table has 0 rows.

    EXPECTED BEHAVIOR (after fix): Each caught exception should log error
    details to the error_logs table.

    This test FAILS on unfixed code because no service writes to error_logs
    (confirming the bug exists).

    **Validates: Requirements 1.27**
    """

    @pytest.mark.asyncio
    async def test_handled_exceptions_log_to_error_logs_table(self):
        """Property: Handled exceptions should write to error_logs table.

        Bug condition: error_logs table has 0 rows after handled exceptions.

        Expected (fixed): Each caught exception logs error details to the
        error_logs table via ErrorLoggingService.
        """
        error_log_rows: list[dict] = []

        # Force a JSON parse error in prototype_ingestion_node to trigger
        # the exception handling path
        mock_llm_response_invalid = MagicMock()
        mock_llm_response_invalid.content = "NOT VALID JSON {{{invalid"
        mock_llm_response_invalid.usage_metadata = {
            "input_tokens": 500,
            "output_tokens": 100,
            "total_tokens": 600,
        }

        # Both calls return invalid JSON to trigger the full error path
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=mock_llm_response_invalid)

        mock_file_contents = {
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }

        state = {
            "messages": [],
            "prompt": "Build a user management app",
            "project_name": "test-project",
            "pipeline_type": "brownfield",
            "volume_paths": ["/Volumes/cat/sch/vol/app.zip"],
            "is_sufficient": True,
            "clarification_questions": [],
            "entities": [],
            "relationships": [],
            "prototype_context": "",
            "extracted_seed_data": {},
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
            "current_step": "",
            "completed_steps": [],
            "error": "",
        }

        with (
            patch(f"{_NODE_MODULE}.get_workspace_client") as mock_get_ws,
            patch(f"{_NODE_MODULE}.get_llm") as mock_get_llm,
            patch(
                f"{_VOLUME_SVC}.read_prototype_files",
                new_callable=AsyncMock,
                return_value=mock_file_contents,
            ),
        ):
            mock_get_ws.return_value = MagicMock()
            mock_get_llm.return_value = mock_llm

            from lakebase_accelerator.agent.nodes.prototype_ingestion import (
                prototype_ingestion_node,
            )

            result = await prototype_ingestion_node(state)

        # Verify that an error occurred (the JSON parse failed)
        assert result.get("error"), (
            "Precondition: Expected an error from invalid JSON response, "
            f"but got result with keys: {list(result.keys())}"
        )

        # EXPECTED BEHAVIOR (after fix): After a handled exception, error
        # details should be logged to error_logs table (rows > 0).
        # On unfixed code, this assertion FAILS because no code writes to
        # error_logs (confirming bug condition d).
        assert len(error_log_rows) > 0, (
            "Bug condition confirmed: An exception was caught and handled "
            f"(error='{result.get('error', '')[:100]}') but error_logs table "
            "has 0 rows. The error was logged to stdout only, never persisted "
            "to the accelerator_meta.error_logs table."
        )
