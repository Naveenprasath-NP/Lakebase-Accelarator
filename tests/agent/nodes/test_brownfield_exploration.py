"""Unit tests for the brownfield exploration node (ReAct-style agent)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lakebase_accelerator.agent.nodes.brownfield_exploration import (
    BROWNFIELD_EXPLORATION_SYSTEM_PROMPT,
    MAX_TOOL_CALL_ITERATIONS,
    _derive_project_name,
    _extract_volume_files,
    _log_error_safe,
    _run_react_loop,
    brownfield_exploration_node,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_state():
    """Create a minimal PipelineState dict for brownfield exploration."""
    return {
        "prompt": "Analyze this Django project",
        "project_name": "my-django-app",
        "pipeline_type": "brownfield",
        "volume_paths": ["/Volumes/catalog/schema/volume/project.zip"],
        "messages": [],
    }


@pytest.fixture
def sample_volume_cache():
    """Create a sample volume cache with typical project files."""
    return {
        "requirements.txt": b"django==4.2\npsycopg2-binary==2.9\n",
        "manage.py": b"#!/usr/bin/env python\nimport os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')\n",
        "app/models.py": b"from django.db import models\n\nclass Customer(models.Model):\n    name = models.CharField(max_length=255)\n    email = models.EmailField(unique=True)\n",
        "app/views.py": b"from django.http import JsonResponse\n\ndef customer_list(request):\n    return JsonResponse({'customers': []})\n",
    }


@pytest.fixture
def mock_llm_response_with_tools():
    """Create a mock LLM response that requests tool calls."""
    response = MagicMock()
    response.content = ""
    response.usage_metadata = {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600}
    response.tool_calls = [
        {
            "name": "list_volume_directory",
            "args": {"path": "."},
            "id": "call_1",
        }
    ]
    return response


@pytest.fixture
def mock_llm_final_response():
    """Create a mock LLM response with final structured output (no tool calls)."""
    result = {
        "project_structure": {
            "root_files": ["manage.py", "requirements.txt"],
            "directories": ["app/"],
            "entrypoint": "manage.py",
            "total_files_analyzed": 4,
        },
        "tech_stack": {
            "language": "Python",
            "framework": "Django",
            "orm": "Django ORM",
            "frontend": None,
            "build_tools": ["pip"],
            "dependencies": ["django", "psycopg2-binary"],
        },
        "entities": [
            {
                "name": "customer",
                "description": "A customer in the system",
                "source_file": "app/models.py",
                "attributes": [
                    {"name": "id", "data_type": "UUID", "nullable": False, "is_primary_key": True, "default_value": "gen_random_uuid()"},
                    {"name": "name", "data_type": "VARCHAR(255)", "nullable": False, "is_primary_key": False, "default_value": None},
                    {"name": "email", "data_type": "VARCHAR(254)", "nullable": False, "is_primary_key": False, "default_value": None},
                    {"name": "created_at", "data_type": "TIMESTAMPTZ", "nullable": False, "is_primary_key": False, "default_value": "NOW()"},
                    {"name": "updated_at", "data_type": "TIMESTAMPTZ", "nullable": False, "is_primary_key": False, "default_value": "NOW()"},
                ],
            }
        ],
        "relationships": [],
        "api_endpoints": [
            {"method": "GET", "path": "/api/customers", "description": "List customers", "request_body": None, "response_shape": "list of customers"}
        ],
        "ui_structure": {"pages": [], "components": [], "navigation": "none", "data_sources": {}},
        "business_logic": [],
        "production_gaps": [
            {"category": "auth", "description": "No authentication implemented", "severity": "critical"}
        ],
        "seed_data_locations": [],
    }
    response = MagicMock()
    response.content = json.dumps(result)
    response.usage_metadata = {"input_tokens": 2000, "output_tokens": 1500, "total_tokens": 3500}
    response.tool_calls = []  # No tool calls = finished
    return response


# ═══════════════════════════════════════════════════════════════════════
# TEST: brownfield_exploration_node
# ═══════════════════════════════════════════════════════════════════════


class TestBrownfieldExplorationNode:
    """Tests for the main brownfield_exploration_node function."""

    @pytest.mark.asyncio
    async def test_returns_error_when_no_volume_paths(self):
        """Test that node returns error when volume_paths is empty."""
        state = {
            "prompt": "Analyze this",
            "project_name": "test",
            "pipeline_type": "brownfield",
            "volume_paths": [],
            "messages": [],
        }

        result = await brownfield_exploration_node(state)

        assert result["error"] == "No files uploaded for brownfield pipeline"
        assert result["current_step"] == "brownfield_exploration"

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    async def test_returns_error_when_extraction_fails(self, mock_extract, sample_state):
        """Test that node returns error when file extraction raises."""
        mock_extract.side_effect = RuntimeError("Volume unavailable")

        with patch("lakebase_accelerator.agent.nodes.brownfield_exploration._log_error_safe"):
            result = await brownfield_exploration_node(sample_state)

        assert "Failed to extract prototype files" in result["error"]
        assert result["current_step"] == "brownfield_exploration"

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    async def test_returns_error_when_no_files_extracted(self, mock_extract, sample_state):
        """Test that node returns error when extraction yields empty cache."""
        mock_extract.return_value = {}

        result = await brownfield_exploration_node(sample_state)

        assert "No readable files found" in result["error"]

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._run_react_loop")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.set_volume_cache")
    async def test_successful_exploration_returns_entities(
        self, mock_set_cache, mock_extract, mock_react, sample_state
    ):
        """Test successful exploration returns entities and relationships."""
        mock_extract.return_value = {"app/models.py": b"class Customer: pass"}

        mock_result = {
            "project_structure": {"root_files": ["manage.py"], "directories": ["app/"], "entrypoint": "manage.py", "total_files_analyzed": 2},
            "tech_stack": {"language": "Python", "framework": "Django", "orm": "Django ORM", "frontend": None, "build_tools": [], "dependencies": []},
            "entities": [{"name": "customer", "description": "A customer", "source_file": "app/models.py", "attributes": []}],
            "relationships": [{"from_entity": "order", "to_entity": "customer", "cardinality": "many_to_one", "foreign_key_column": "customer_id"}],
            "api_endpoints": [],
            "ui_structure": {},
            "business_logic": [],
            "production_gaps": [],
            "seed_data_locations": [],
        }
        mock_react.return_value = (mock_result, 5, 3000, 2000)

        result = await brownfield_exploration_node(sample_state)

        assert result["entities"] == mock_result["entities"]
        assert result["relationships"] == mock_result["relationships"]
        assert result["project_structure"] == mock_result["project_structure"]
        assert result["tech_stack"] == mock_result["tech_stack"]
        assert result["tool_call_count"] == 5
        assert result["total_input_tokens"] == 3000
        assert result["total_output_tokens"] == 2000
        assert result["current_step"] == "brownfield_exploration"
        assert "brownfield_exploration" in result["completed_steps"]

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._run_react_loop")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.set_volume_cache")
    async def test_preserves_existing_project_name(
        self, mock_set_cache, mock_extract, mock_react, sample_state
    ):
        """Test that existing project_name in state is preserved."""
        mock_extract.return_value = {"file.py": b"content"}
        mock_react.return_value = (
            {"project_structure": {}, "tech_stack": {}, "entities": [], "relationships": []},
            2, 1000, 500,
        )

        result = await brownfield_exploration_node(sample_state)

        assert result["project_name"] == "my-django-app"

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._run_react_loop")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.set_volume_cache")
    async def test_react_loop_failure_returns_error(
        self, mock_set_cache, mock_extract, mock_react, sample_state
    ):
        """Test that ReAct loop failure is handled gracefully."""
        mock_extract.return_value = {"file.py": b"content"}
        mock_react.side_effect = Exception("LLM timeout")

        with patch("lakebase_accelerator.agent.nodes.brownfield_exploration._log_error_safe"):
            result = await brownfield_exploration_node(sample_state)

        assert "Brownfield exploration agent failed" in result["error"]

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._run_react_loop")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration._extract_volume_files")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.set_volume_cache")
    async def test_populates_volume_cache(
        self, mock_set_cache, mock_extract, mock_react, sample_state
    ):
        """Test that volume cache is populated with extracted files."""
        cache_data = {"file1.py": b"content1", "file2.py": b"content2"}
        mock_extract.return_value = cache_data
        mock_react.return_value = (
            {"project_structure": {}, "tech_stack": {}, "entities": [], "relationships": []},
            1, 500, 200,
        )

        await brownfield_exploration_node(sample_state)

        mock_set_cache.assert_called_once_with(cache_data)


# ═══════════════════════════════════════════════════════════════════════
# TEST: _extract_volume_files
# ═══════════════════════════════════════════════════════════════════════


class TestExtractVolumeFiles:
    """Tests for the _extract_volume_files helper."""

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_workspace_client")
    async def test_extracts_zip_file(self, mock_get_client):
        """Test that zip files are extracted correctly."""
        import io
        import zipfile as zf

        # Create a zip file in memory
        zip_buffer = io.BytesIO()
        with zf.ZipFile(zip_buffer, "w") as z:
            z.writestr("src/main.py", "print('hello')")
            z.writestr("README.md", "# Project")

        zip_buffer.seek(0)

        # Mock workspace client
        mock_response = MagicMock()
        mock_response.contents.read.return_value = zip_buffer.getvalue()
        mock_client = MagicMock()
        mock_client.files.download.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await _extract_volume_files(["/Volumes/cat/sch/vol/project.zip"])

        assert "src/main.py" in result
        assert result["src/main.py"] == b"print('hello')"
        assert "README.md" in result

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_workspace_client")
    async def test_reads_single_text_file(self, mock_get_client):
        """Test that non-zip files are read directly."""
        mock_response = MagicMock()
        mock_response.contents.read.return_value = b"file content"
        mock_client = MagicMock()
        mock_client.files.download.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await _extract_volume_files(["/Volumes/cat/sch/vol/app.py"])

        assert "app.py" in result
        assert result["app.py"] == b"file content"

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_workspace_client")
    async def test_skips_macosx_entries_in_zip(self, mock_get_client):
        """Test that __MACOSX entries are skipped during zip extraction."""
        import io
        import zipfile as zf

        zip_buffer = io.BytesIO()
        with zf.ZipFile(zip_buffer, "w") as z:
            z.writestr("src/main.py", "code")
            z.writestr("__MACOSX/._main.py", "metadata")

        zip_buffer.seek(0)

        mock_response = MagicMock()
        mock_response.contents.read.return_value = zip_buffer.getvalue()
        mock_client = MagicMock()
        mock_client.files.download.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await _extract_volume_files(["/Volumes/cat/sch/vol/project.zip"])

        assert "src/main.py" in result
        assert "__MACOSX/._main.py" not in result

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_workspace_client")
    async def test_handles_download_failure_gracefully(self, mock_get_client):
        """Test that single file download failures are handled gracefully."""
        mock_client = MagicMock()
        mock_client.files.download.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        result = await _extract_volume_files(["/Volumes/cat/sch/vol/app.py"])

        # Should return empty dict, not raise
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
# TEST: _run_react_loop
# ═══════════════════════════════════════════════════════════════════════


class TestRunReactLoop:
    """Tests for the ReAct agent loop."""

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.invoke_with_logging")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_llm")
    async def test_immediate_final_response(self, mock_get_llm, mock_invoke, mock_llm_final_response):
        """Test that loop ends immediately when LLM responds without tool calls."""
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_get_llm.return_value = mock_llm
        mock_invoke.return_value = mock_llm_final_response

        result, tool_count, input_tokens, output_tokens = await _run_react_loop(
            project_id="test-project",
            user_prompt="Analyze this",
        )

        assert tool_count == 0
        assert "entities" in result
        assert input_tokens == 2000
        assert output_tokens == 1500

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.invoke_with_logging")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_llm")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.set_volume_cache")
    async def test_tool_call_then_final_response(
        self, mock_set_cache, mock_get_llm, mock_invoke, mock_llm_response_with_tools, mock_llm_final_response
    ):
        """Test that loop executes tool calls and then gets final response."""
        from lakebase_accelerator.agent.tools.brownfield_tools import set_volume_cache

        # Set up volume cache for the tool to work with
        set_volume_cache({"file.py": b"content"})

        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_get_llm.return_value = mock_llm

        # First call returns tool request, second call returns final response
        mock_invoke.side_effect = [mock_llm_response_with_tools, mock_llm_final_response]

        result, tool_count, input_tokens, output_tokens = await _run_react_loop(
            project_id="test-project",
            user_prompt="Analyze this",
        )

        assert tool_count == 1
        assert "entities" in result
        # Tokens from both calls
        assert input_tokens == 2500  # 500 + 2000
        assert output_tokens == 1600  # 100 + 1500

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.invoke_with_logging")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_llm")
    async def test_max_iterations_produces_final_output(self, mock_get_llm, mock_invoke):
        """Test that hitting max iterations triggers a final output request."""
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_get_llm.return_value = mock_llm

        # Create a response that always wants to call tools
        tool_response = MagicMock()
        tool_response.content = ""
        tool_response.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        tool_response.tool_calls = [
            {"name": "list_volume_directory", "args": {"path": "."}, "id": "call_x"}
        ]

        # Final response after hitting limit
        final_response = MagicMock()
        final_response.content = json.dumps({
            "project_structure": {},
            "tech_stack": {},
            "entities": [],
            "relationships": [],
            "api_endpoints": [],
            "ui_structure": {},
            "business_logic": [],
            "production_gaps": [],
            "seed_data_locations": [],
        })
        final_response.usage_metadata = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        final_response.tool_calls = []

        # Set up volume cache for tool execution
        from lakebase_accelerator.agent.tools.brownfield_tools import set_volume_cache
        set_volume_cache({"file.py": b"content"})

        # Return tool_response for MAX iterations, then final_response
        mock_invoke.side_effect = [tool_response] * MAX_TOOL_CALL_ITERATIONS + [final_response]

        result, tool_count, input_tokens, output_tokens = await _run_react_loop(
            project_id="test-project",
            user_prompt="Analyze this",
        )

        assert tool_count == MAX_TOOL_CALL_ITERATIONS
        assert "entities" in result

    @pytest.mark.asyncio
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.invoke_with_logging")
    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_llm")
    async def test_handles_unknown_tool_gracefully(self, mock_get_llm, mock_invoke):
        """Test that unknown tool names are handled without crashing."""
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_llm
        mock_get_llm.return_value = mock_llm

        # Response requesting an unknown tool
        unknown_tool_response = MagicMock()
        unknown_tool_response.content = ""
        unknown_tool_response.usage_metadata = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        unknown_tool_response.tool_calls = [
            {"name": "nonexistent_tool", "args": {"path": "."}, "id": "call_1"}
        ]

        # Final response
        final_response = MagicMock()
        final_response.content = json.dumps({"project_structure": {}, "tech_stack": {}, "entities": [], "relationships": []})
        final_response.usage_metadata = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        final_response.tool_calls = []

        mock_invoke.side_effect = [unknown_tool_response, final_response]

        result, tool_count, _, _ = await _run_react_loop(
            project_id="test-project",
            user_prompt="Analyze this",
        )

        assert tool_count == 1
        assert "entities" in result


# ═══════════════════════════════════════════════════════════════════════
# TEST: _derive_project_name
# ═══════════════════════════════════════════════════════════════════════


class TestDeriveProjectName:
    """Tests for project name derivation."""

    def test_derives_from_entrypoint_directory(self):
        """Test deriving name from entrypoint path."""
        structure = {"entrypoint": "myproject/main.py"}
        tech_stack = {}

        result = _derive_project_name(structure, tech_stack)

        assert result == "myproject"

    def test_derives_from_framework(self):
        """Test deriving name from framework when entrypoint is generic."""
        structure = {"entrypoint": "src/main.py"}
        tech_stack = {"framework": "Django"}

        result = _derive_project_name(structure, tech_stack)

        assert result == "django-prototype"

    def test_derives_from_language(self):
        """Test deriving name from language when no framework."""
        structure = {}
        tech_stack = {"language": "Python", "framework": ""}

        result = _derive_project_name(structure, tech_stack)

        assert result == "python-prototype"

    def test_returns_default_when_no_info(self):
        """Test fallback to unnamed-prototype."""
        result = _derive_project_name({}, {})

        assert result == "unnamed-prototype"

    def test_skips_generic_directory_names(self):
        """Test that generic dirs like src/app/lib are skipped."""
        structure = {"entrypoint": "src/main.py"}
        tech_stack = {}

        result = _derive_project_name(structure, tech_stack)

        # Should not use "src" as project name
        assert result == "unnamed-prototype"


# ═══════════════════════════════════════════════════════════════════════
# TEST: _log_error_safe
# ═══════════════════════════════════════════════════════════════════════


class TestLogErrorSafe:
    """Tests for the fire-and-forget error logging helper."""

    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_error_logging_service")
    def test_calls_error_logging_service(self, mock_get_service):
        """Test that error is logged via ErrorLoggingService."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        _log_error_safe(
            project_id="test-id",
            error_code="TEST_ERROR",
            severity="high",
            source_component="test_component",
            source_step="test_step",
            error_message="Something went wrong",
        )

        mock_service.log_error.assert_called_once()
        call_kwargs = mock_service.log_error.call_args[1]
        assert call_kwargs["project_id"] == "test-id"
        assert call_kwargs["error_code"] == "TEST_ERROR"
        assert call_kwargs["severity"] == "high"

    @patch("lakebase_accelerator.agent.nodes.brownfield_exploration.get_error_logging_service")
    def test_does_not_raise_on_service_failure(self, mock_get_service):
        """Test that service failures are swallowed (fire-and-forget)."""
        mock_get_service.side_effect = RuntimeError("DB unavailable")

        # Should not raise
        _log_error_safe(
            project_id="test-id",
            error_code="TEST_ERROR",
            severity="high",
            source_component="test_component",
            source_step="test_step",
            error_message="Something went wrong",
        )


# ═══════════════════════════════════════════════════════════════════════
# TEST: System Prompt & Constants
# ═══════════════════════════════════════════════════════════════════════


class TestConstants:
    """Tests for module constants and system prompt structure."""

    def test_max_iterations_is_30(self):
        """Test that max tool call iterations is set to 30."""
        assert MAX_TOOL_CALL_ITERATIONS == 30

    def test_system_prompt_has_persona_section(self):
        """Test that system prompt includes persona section."""
        assert "## Persona" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT

    def test_system_prompt_has_task_scope_section(self):
        """Test that system prompt includes task scope section."""
        assert "## Task Scope" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT

    def test_system_prompt_has_constraints_section(self):
        """Test that system prompt includes constraints section."""
        assert "## Constraints" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT

    def test_system_prompt_has_output_format_section(self):
        """Test that system prompt includes output format section."""
        assert "## Output Format" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT

    def test_system_prompt_mentions_skip_directories(self):
        """Test that system prompt instructs to skip irrelevant directories."""
        assert "node_modules" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT
        assert ".git" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT
        assert "dist" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT

    def test_system_prompt_mentions_max_tool_calls(self):
        """Test that system prompt mentions the iteration limit."""
        assert "30" in BROWNFIELD_EXPLORATION_SYSTEM_PROMPT
