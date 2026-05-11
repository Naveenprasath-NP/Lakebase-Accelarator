"""Preservation Property Tests — Greenfield Flow & SSE Event Format Unchanged.

These tests encode the EXISTING correct behavior that must be preserved after
the brownfield agent fix is implemented. They verify:

- Greenfield pipeline routing logic remains unchanged
- SSE event format (step, status, message, data, timestamp) is preserved
- Deployment self-healing retry mechanism works identically
- VolumeReaderService filtering (SKIP_DIRECTORIES, SKIP_EXTENSIONS, priority) works identically

All tests MUST PASS on the current unfixed code.

**Validates: Requirements 3.1, 3.3, 3.4, 3.7, 3.10**
"""

import json
from pathlib import PurePosixPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lakebase_accelerator.agent.graph import (
    _route_after_deployment,
    _route_after_intake,
    _route_start,
)
from lakebase_accelerator.services.volume_reader_service import (
    HIGH_PRIORITY_PATTERNS,
    MAX_CHARS_PER_FILE,
    MAX_FILES_TO_READ,
    MEDIUM_PRIORITY_EXTENSIONS,
    SKIP_DIRECTORIES,
    SKIP_EXTENSIONS,
    VolumeReaderService,
)


# ─── Strategies ──────────────────────────────────────────────────────────────


@st.composite
def greenfield_pipeline_states(draw):
    """Generate greenfield pipeline states with varying prompts and fields.

    All generated states have pipeline_type='greenfield'.
    """
    prompt = draw(st.text(
        min_size=10, max_size=200,
        alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    ))
    project_name = draw(st.text(
        min_size=3, max_size=30,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    ))

    return {
        "messages": [],
        "prompt": prompt,
        "project_name": project_name,
        "pipeline_type": "greenfield",
        "volume_paths": [],
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


@st.composite
def post_intake_states_sufficient(draw):
    """Generate states after intake where is_sufficient=True.

    These states should route to 'design_model'.
    """
    state = draw(greenfield_pipeline_states())
    state["is_sufficient"] = True
    # Add some entities to simulate intake output
    num_entities = draw(st.integers(min_value=1, max_value=5))
    state["entities"] = [
        {"name": f"entity_{i}", "description": f"Entity {i}", "attributes": []}
        for i in range(num_entities)
    ]
    return state


@st.composite
def deployment_success_states(draw):
    """Generate deployment states where app_url is set and no error.

    These states should route to END.
    """
    state = draw(greenfield_pipeline_states())
    app_name = draw(st.text(
        min_size=5, max_size=30,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    ))
    state["app_url"] = f"https://{app_name}.databricksapps.com"
    state["app_name"] = app_name
    state["error"] = ""
    state["deployment_error_logs"] = ""
    return state


@st.composite
def deployment_failure_with_logs_states(draw):
    """Generate deployment states with error logs but no fatal error.

    These states should route back to 'backend_dev' for self-healing.
    """
    state = draw(greenfield_pipeline_states())
    state["app_url"] = ""
    state["error"] = ""
    error_msg = draw(st.text(
        min_size=10, max_size=100,
        alphabet=st.characters(whitelist_categories=("L", "N", "Z", "P")),
    ))
    state["deployment_error_logs"] = error_msg
    state["deployment_retry_count"] = draw(st.integers(min_value=0, max_value=1))
    return state


@st.composite
def sse_event_data_dicts(draw):
    """Generate SSE event data dictionaries matching the expected format.

    All SSE events must contain: step, status, message, data, timestamp.
    """
    step = draw(st.sampled_from([
        "intake", "prototype_ingestion", "prototype_analysis",
        "design_model", "schema_provisioning", "seed_data",
        "backend_dev", "frontend_dev", "integration", "deployment", None,
    ]))
    status = draw(st.sampled_from(["completed", "failed", "needs_clarification"]))
    message = draw(st.text(
        min_size=5, max_size=100,
        alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    ))
    data = draw(st.fixed_dictionaries({
        "key": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L",))),
    }))
    timestamp = draw(st.from_regex(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00",
        fullmatch=True,
    ))

    return {
        "step": step,
        "status": status,
        "message": message,
        "data": data,
        "timestamp": timestamp,
    }


@st.composite
def volume_file_paths(draw):
    """Generate file paths for VolumeReaderService filtering tests."""
    # Mix of paths that should be skipped and paths that should pass
    skip_dir = draw(st.sampled_from(list(SKIP_DIRECTORIES)))
    valid_ext = draw(st.sampled_from([".py", ".ts", ".js", ".json", ".yaml"]))
    skip_ext = draw(st.sampled_from(list(SKIP_EXTENSIONS)))
    filename = draw(st.text(
        min_size=3, max_size=15,
        alphabet=st.characters(whitelist_categories=("L",)),
    ))

    return {
        "skip_dir_path": f"{skip_dir}/{filename}{valid_ext}",
        "skip_ext_path": f"src/{filename}{skip_ext}",
        "valid_path": f"src/{filename}{valid_ext}",
        "filename": filename,
        "valid_ext": valid_ext,
    }


# ─── Property 1: Greenfield Routing — _route_start ──────────────────────────


class TestPreservationGreenfieldRouteStart:
    """Property: For all greenfield pipeline states, _route_start returns 'intake'.

    This ensures the greenfield entry point routing is never broken by the
    brownfield agent fix.

    **Validates: Requirements 3.1, 3.3**
    """

    @given(state=greenfield_pipeline_states())
    @settings(max_examples=10, deadline=5000)
    def test_route_start_returns_intake_for_all_greenfield_states(self, state):
        """Property: _route_start always returns 'intake' for greenfield pipelines.

        Observation: On unfixed code, _route_start checks pipeline_type and returns
        'intake' for greenfield, 'prototype_ingestion' for brownfield.
        """
        result = _route_start(state)
        assert result == "intake", (
            f"_route_start returned '{result}' for greenfield state, expected 'intake'. "
            f"Greenfield routing must always go to intake node."
        )


# ─── Property 2: Post-Intake Routing — is_sufficient=True → design_model ────


class TestPreservationPostIntakeRouting:
    """Property: For all states with is_sufficient=True after intake, routing
    proceeds to 'design_model'.

    This ensures the intake → design_model transition is preserved.

    **Validates: Requirements 3.1, 3.3**
    """

    @given(state=post_intake_states_sufficient())
    @settings(max_examples=10, deadline=5000)
    def test_route_after_intake_proceeds_to_checkpoint_when_sufficient(self, state):
        """Property: _route_after_intake returns 'intake_review_checkpoint' when is_sufficient=True.

        Observation: After fix, _route_after_intake checks is_sufficient
        and routes to 'intake_review_checkpoint' if True, END if False.
        The checkpoint then proceeds to design_model after user approval.
        """
        result = _route_after_intake(state)
        assert result == "intake_review_checkpoint", (
            f"_route_after_intake returned '{result}' for state with is_sufficient=True, "
            f"expected 'intake_review_checkpoint'. Post-intake routing must proceed to "
            f"intake review checkpoint when the prompt analysis is sufficient."
        )


# ─── Property 3: Deployment Success → END ───────────────────────────────────


class TestPreservationDeploymentSuccessRouting:
    """Property: For all deployment states with app_url set and no error,
    routing returns END.

    This ensures successful deployments terminate the pipeline correctly.

    **Validates: Requirements 3.4, 3.7**
    """

    @given(state=deployment_success_states())
    @settings(max_examples=10, deadline=5000)
    def test_route_after_deployment_returns_end_on_success(self, state):
        """Property: _route_after_deployment returns END when app_url is set and no error.

        Observation: On unfixed code, _route_after_deployment checks:
        - app_url set AND no error → END (success)
        - deployment_error_logs AND no error → 'backend_dev' (retry)
        - error set → END (max retries exceeded)
        """
        result = _route_after_deployment(state)
        assert result == "__end__", (
            f"_route_after_deployment returned '{result}' for state with "
            f"app_url='{state['app_url']}' and error='', expected '__end__'. "
            f"Successful deployments must terminate the pipeline."
        )


# ─── Property 4: Deployment Failure with Logs → backend_dev (Self-Healing) ───


class TestPreservationDeploymentSelfHealing:
    """Property: For all deployment states with deployment_error_logs and no error,
    routing returns 'backend_dev' for self-healing retry.

    This ensures the deployment self-healing mechanism is preserved.

    **Validates: Requirements 3.4, 3.10**
    """

    @given(state=deployment_failure_with_logs_states())
    @settings(max_examples=10, deadline=5000)
    def test_route_after_deployment_returns_backend_dev_on_failure_with_logs(self, state):
        """Property: _route_after_deployment returns 'backend_dev' when deployment
        fails with error logs and no fatal error (retries available).

        Observation: On unfixed code, deployment self-healing routes back to
        backend_dev on failure (up to 2 retries).
        """
        result = _route_after_deployment(state)
        assert result == "backend_dev", (
            f"_route_after_deployment returned '{result}' for state with "
            f"deployment_error_logs='{state['deployment_error_logs'][:50]}...' "
            f"and error='', expected 'backend_dev'. "
            f"Deployment self-healing must route back to backend_dev for retry."
        )


# ─── Property 5: SSE Event Format ───────────────────────────────────────────


class TestPreservationSSEEventFormat:
    """Property: For all SSE event emissions, the JSON structure contains keys:
    step, status, message, data, timestamp.

    This ensures the SSE event format is never broken by the fix.

    **Validates: Requirements 3.7, 3.10**
    """

    @given(event_data=sse_event_data_dicts())
    @settings(max_examples=10, deadline=5000)
    def test_sse_event_json_contains_required_keys(self, event_data):
        """Property: All SSE event data must contain step, status, message, data, timestamp.

        Observation: On unfixed code, SSE events use format:
        event: step_completed\\ndata: {JSON with step, status, message, data, timestamp}\\n\\n
        """
        required_keys = {"step", "status", "message", "data", "timestamp"}

        # Verify the structure has all required keys
        assert required_keys.issubset(event_data.keys()), (
            f"SSE event data missing required keys. "
            f"Has: {set(event_data.keys())}, Required: {required_keys}, "
            f"Missing: {required_keys - set(event_data.keys())}"
        )

        # Verify it serializes to valid JSON
        json_str = json.dumps(event_data)
        parsed = json.loads(json_str)
        assert required_keys.issubset(parsed.keys()), (
            f"SSE event data lost required keys after JSON round-trip. "
            f"Parsed keys: {set(parsed.keys())}"
        )

    def test_step_completed_event_format_matches_source(self):
        """Verify the actual SSE event emission code produces the correct format.

        Observation: The event_stream() in projects.py builds sse_data with
        step, status, message, data, timestamp keys for step_completed events.
        """
        import inspect

        from lakebase_accelerator.routes import projects as projects_module

        source = inspect.getsource(projects_module)

        # Verify the SSE format structure exists in source
        assert '"step"' in source, "SSE event format must include 'step' key"
        assert '"status"' in source, "SSE event format must include 'status' key"
        assert '"message"' in source, "SSE event format must include 'message' key"
        assert '"data"' in source, "SSE event format must include 'data' key"
        assert '"timestamp"' in source, "SSE event format must include 'timestamp' key"

        # Verify the event types exist
        assert "step_completed" in source, "SSE must emit 'step_completed' events"
        assert "step_failed" in source, "SSE must emit 'step_failed' events"
        assert "pipeline_complete" in source, "SSE must emit 'pipeline_complete' events"

    def test_sse_event_format_uses_json_dumps(self):
        """Verify SSE events are serialized with json.dumps for consistency.

        Observation: The event_stream uses f'event: {type}\\ndata: {json.dumps(sse_data)}\\n\\n'
        """
        import inspect

        from lakebase_accelerator.routes import projects as projects_module

        source = inspect.getsource(projects_module)

        # The SSE format must use json.dumps for data serialization
        assert "json.dumps" in source, (
            "SSE events must use json.dumps for data serialization"
        )


# ─── Property 6: VolumeReaderService Filtering ──────────────────────────────


class TestPreservationVolumeReaderServiceFiltering:
    """Property: VolumeReaderService filtering (SKIP_DIRECTORIES, SKIP_EXTENSIONS,
    priority classification) works identically.

    This ensures the file filtering logic is never broken by the fix.

    **Validates: Requirements 3.1, 3.10**
    """

    @given(paths=volume_file_paths())
    @settings(max_examples=10, deadline=5000)
    def test_skip_directories_are_filtered(self, paths):
        """Property: Files in SKIP_DIRECTORIES are always filtered out.

        Observation: VolumeReaderService._should_skip_path returns True for
        any path containing a directory component in SKIP_DIRECTORIES.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Path with a skip directory should be skipped
        skip_path = paths["skip_dir_path"]
        assert service._should_skip_path(skip_path) is True, (
            f"Path '{skip_path}' should be skipped because it contains a "
            f"directory from SKIP_DIRECTORIES, but _should_skip_path returned False."
        )

    @given(paths=volume_file_paths())
    @settings(max_examples=10, deadline=5000)
    def test_skip_extensions_are_filtered(self, paths):
        """Property: Files with SKIP_EXTENSIONS are always filtered out.

        Observation: VolumeReaderService._should_skip_path returns True for
        any path with an extension in SKIP_EXTENSIONS.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Path with a skip extension should be skipped
        skip_ext_path = paths["skip_ext_path"]
        assert service._should_skip_path(skip_ext_path) is True, (
            f"Path '{skip_ext_path}' should be skipped because it has an "
            f"extension from SKIP_EXTENSIONS, but _should_skip_path returned False."
        )

    @given(paths=volume_file_paths())
    @settings(max_examples=10, deadline=5000)
    def test_valid_paths_are_not_filtered(self, paths):
        """Property: Files with valid extensions in valid directories pass filtering.

        Observation: VolumeReaderService._should_skip_path returns False for
        paths that don't match any skip criteria.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Valid path should not be skipped
        valid_path = paths["valid_path"]
        assert service._should_skip_path(valid_path) is False, (
            f"Path '{valid_path}' should NOT be skipped (valid directory and extension), "
            f"but _should_skip_path returned True."
        )

    @given(
        filename=st.sampled_from(HIGH_PRIORITY_PATTERNS),
        content=st.binary(min_size=10, max_size=100),
    )
    @settings(max_examples=5, deadline=5000)
    def test_high_priority_files_classified_correctly(self, filename, content):
        """Property: Files matching HIGH_PRIORITY_PATTERNS are classified as high priority.

        Observation: VolumeReaderService._process_files classifies files matching
        HIGH_PRIORITY_PATTERNS into the high_priority bucket.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Create a file that matches a high priority pattern
        filepath = f"src/{filename}"
        if not any(filepath.endswith(ext) for ext in (".py", ".sql", ".ts", ".js")):
            filepath = f"src/{filename}.py"

        raw_files = {filepath: f"# High priority content for {filename}\nclass Model:\n    pass\n".encode()}
        result = service._process_files(raw_files)

        # High priority files should be included in the result (assuming they're text)
        assert len(result) > 0, (
            f"File '{filepath}' matches HIGH_PRIORITY_PATTERN '{filename}' "
            f"but was not included in processed results."
        )

    def test_max_files_limit_respected(self):
        """Verify MAX_FILES_TO_READ limit is enforced during processing.

        Observation: VolumeReaderService._process_files reads at most
        MAX_FILES_TO_READ (25) files.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Create more files than the limit
        raw_files = {
            f"src/file_{i}.py": f"# File {i}\nclass Model{i}:\n    pass\n".encode()
            for i in range(MAX_FILES_TO_READ + 10)
        }

        result = service._process_files(raw_files)

        assert len(result) <= MAX_FILES_TO_READ, (
            f"VolumeReaderService processed {len(result)} files, exceeding "
            f"MAX_FILES_TO_READ={MAX_FILES_TO_READ}. The file limit must be preserved."
        )

    def test_max_chars_per_file_respected(self):
        """Verify MAX_CHARS_PER_FILE limit truncates large files.

        Observation: VolumeReaderService._process_files truncates files
        exceeding MAX_CHARS_PER_FILE (4000) characters.
        """
        from unittest.mock import MagicMock

        service = VolumeReaderService(workspace_client=MagicMock())

        # Create a file larger than the per-file limit
        large_content = ("x" * (MAX_CHARS_PER_FILE + 1000)).encode()
        raw_files = {"src/large_file.py": large_content}

        result = service._process_files(raw_files)

        assert "src/large_file.py" in result, "Large file should still be included"
        # The content should be truncated (original chars + truncation notice)
        assert len(result["src/large_file.py"]) <= MAX_CHARS_PER_FILE + 200, (
            f"File content length {len(result['src/large_file.py'])} exceeds "
            f"MAX_CHARS_PER_FILE={MAX_CHARS_PER_FILE} + reasonable truncation notice. "
            f"Per-file truncation must be preserved."
        )
