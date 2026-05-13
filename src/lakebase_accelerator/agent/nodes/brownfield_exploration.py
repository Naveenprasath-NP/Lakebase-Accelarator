"""Brownfield Exploration Node — ReAct-style iterative agent for codebase analysis.

Implements a tool-calling agent loop that iteratively explores uploaded prototype
code using brownfield tools (list_volume_directory, read_volume_file, etc.).
The agent decides what to look at next based on what it has learned so far,
building a comprehensive understanding of the project structure, tech stack,
entities, relationships, API endpoints, UI structure, and production gaps.

This replaces the single-shot prototype_ingestion approach with an intelligent,
iterative exploration strategy.
"""

import asyncio
import json
import traceback
import zipfile
from io import BytesIO

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from lakebase_accelerator.agent.llm import get_llm, invoke_with_logging
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.agent.tools.brownfield_tools import BROWNFIELD_TOOLS, set_volume_cache
from lakebase_accelerator.services.dependencies import get_error_logging_service, get_workspace_client
from lakebase_accelerator.utils.json_repair import parse_llm_json
from lakebase_accelerator.utils.logger import logger

# ─── Constants ────────────────────────────────────────────────────────

MAX_TOOL_CALL_ITERATIONS = 30
"""Maximum number of tool call iterations to prevent infinite loops."""

# ─── System Prompt ────────────────────────────────────────────────────

BROWNFIELD_EXPLORATION_SYSTEM_PROMPT = """## Persona
You are a senior software architect specializing in reverse engineering and codebase analysis. \
You methodically explore codebases to extract complete application structure, data models, and business logic.

## Task Scope
Analyze the uploaded prototype codebase by iteratively exploring its files and directories. \
Build a comprehensive understanding of the project by:
1. First listing the root directory to understand the project layout
2. Reading configuration/manifest files (package.json, requirements.txt, pyproject.toml, etc.) to identify the tech stack
3. Identifying and skipping irrelevant directories (node_modules, .git, dist, build, __pycache__, .venv, coverage, .next, target)
4. Reading model/entity/schema files to extract entities with their attributes and relationships
5. Reading route/controller files to extract API endpoints
6. Reading frontend component files to understand UI structure
7. Reading service/business logic files to understand validation rules and workflows
8. Identifying production gaps (missing validation, auth, error handling, security, logging)
9. Locating seed data, fixtures, or hardcoded data

## Constraints
- Use the provided tools to explore the codebase iteratively — do NOT try to read all files at once
- Skip binary files and irrelevant directories
- For large files (>50KB), use read_volume_file_section to read specific sections
- Focus on source code directories (src/, app/, lib/, pages/, components/, routes/, models/)
- Prioritize: config files → model/schema files → routes → services → frontend → tests
- Do NOT guess or hallucinate file contents — only report what you actually read from the tools
- Maximum tool calls allowed: 30 — plan your exploration efficiently

## Output Format
When you have completed your exploration, respond with a JSON object (no markdown fences) containing:
{
  "project_structure": {
    "root_files": ["list of top-level files"],
    "directories": ["list of source directories found"],
    "entrypoint": "main application file path",
    "total_files_analyzed": 0
  },
  "tech_stack": {
    "language": "primary language",
    "framework": "web framework",
    "orm": "ORM/database library if any",
    "frontend": "frontend framework if any",
    "build_tools": ["list of build tools"],
    "dependencies": ["key dependencies"]
  },
  "entities": [
    {
      "name": "entity_name_snake_case",
      "description": "what this entity represents",
      "source_file": "where it was found",
      "attributes": [
        {
          "name": "attribute_name",
          "data_type": "PostgreSQL type (UUID, TEXT, VARCHAR(n), INTEGER, BOOLEAN, TIMESTAMPTZ, etc.)",
          "nullable": true,
          "is_primary_key": false,
          "default_value": null
        }
      ]
    }
  ],
  "relationships": [
    {
      "from_entity": "child_entity",
      "to_entity": "parent_entity",
      "cardinality": "one_to_one|one_to_many|many_to_many",
      "foreign_key_column": "parent_id"
    }
  ],
  "api_endpoints": [
    {
      "method": "GET|POST|PUT|DELETE",
      "path": "/api/path",
      "description": "what it does",
      "request_body": "brief description or null",
      "response_shape": "brief description"
    }
  ],
  "ui_structure": {
    "pages": ["list of UI pages/views"],
    "components": ["key reusable components"],
    "navigation": "navigation pattern description",
    "data_sources": {"page_name": "api_endpoint_it_uses"}
  },
  "business_logic": [
    {
      "name": "rule or workflow name",
      "description": "what it does",
      "source_file": "where it was found"
    }
  ],
  "production_gaps": [
    {
      "category": "security|validation|auth|error_handling|logging|monitoring|performance|testing",
      "description": "what is missing",
      "severity": "critical|high|medium|low"
    }
  ],
  "seed_data_locations": ["file paths containing seed/fixture/hardcoded data"],
  "theme": {
    "mode": "dark or light — detect from UI screenshots or CSS. Use 'light' if background is white/light-colored, 'dark' if background is dark/navy/black",
    "brand_color": "#hex — primary/accent color detected from the UI (buttons, active nav items, links)",
    "brand_name": "human-readable color name (e.g., red, green, blue, orange, purple)"
  }
}

Ensure every entity has id (UUID PK), created_at (TIMESTAMPTZ), and updated_at (TIMESTAMPTZ) attributes. \
Use snake_case for all entity and attribute names. Use PostgreSQL data types."""


# ═══════════════════════════════════════════════════════════════════════
# NODE IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════


async def brownfield_exploration_node(state: PipelineState) -> dict:
    """Iteratively explore uploaded prototype code using a ReAct agent loop.

    Supports multimodal inputs:
    - Code files (.py, .ts, .js, etc.) and zips → explored via tools
    - Images (.png, .jpg, .jpeg) → sent to Claude as vision inputs for UI analysis
    - PDFs (.pdf) → text extracted and included as context

    Steps:
    1. Extract files from volume_paths into memory
    2. Classify files by type (code, image, PDF)
    3. Populate the volume cache with code files for tool access
    4. Build multimodal context (images as base64, PDF text)
    5. Run the ReAct loop with multimodal initial message
    6. Parse the final structured output from the LLM
    7. Return state updates

    Args:
        state: Current pipeline state with volume_paths and prompt.

    Returns:
        Dict of state updates including entities, relationships, project_structure, tech_stack.
    """
    logger.info(
        "Node: brownfield_exploration — starting iterative codebase analysis",
        extra={"step": "brownfield_exploration", "volume_paths": len(state.get("volume_paths", []))},
    )

    project_id = state.get("project_id", "") or state.get("project_name", "")
    volume_paths = state.get("volume_paths", [])

    if not volume_paths:
        logger.error("No volume paths in state for brownfield exploration")
        return {
            "error": "No files uploaded for brownfield pipeline",
            "current_step": "brownfield_exploration",
            "completed_steps": [],
        }

    # ─── Step 1: Extract files from volume into memory cache ──────────
    try:
        volume_cache = await _extract_volume_files(volume_paths)
    except Exception as e:
        error_msg = f"Failed to extract prototype files: {str(e)[:200]}"
        logger.exception(error_msg)
        _log_error_safe(
            project_id=project_id,
            error_code="VOLUME_EXTRACTION_ERROR",
            severity="critical",
            source_component="brownfield_exploration_node",
            source_step="brownfield_exploration",
            error_message=error_msg,
            stack_trace=traceback.format_exc(),
        )
        return {
            "error": error_msg,
            "current_step": "brownfield_exploration",
            "completed_steps": [],
        }

    if not volume_cache:
        logger.warning("No files extracted from uploaded prototype")
        return {
            "error": "No readable files found in the uploaded prototype",
            "current_step": "brownfield_exploration",
            "completed_steps": [],
        }

    # ─── Step 2: Classify files by type ───────────────────────────────
    code_files, image_files, pdf_files = _classify_files(volume_cache)

    # Allow pipeline to proceed if we have ANY useful input (code, images, or PDFs)
    if not code_files and not image_files and not pdf_files:
        logger.warning("No usable files found in uploaded prototype")
        return {
            "error": "No usable files found in the uploaded prototype (no code, images, or PDFs)",
            "current_step": "brownfield_exploration",
            "completed_steps": [],
        }

    logger.info(
        f"Files classified: {len(code_files)} code, {len(image_files)} images, {len(pdf_files)} PDFs",
        extra={"step": "brownfield_exploration"},
    )

    # ─── Step 3: Extract text from PDFs ───────────────────────────────
    pdf_texts = _extract_pdf_texts(pdf_files)

    # ─── Step 4: Populate volume cache with code files for tools ──────
    set_volume_cache(code_files)
    logger.info(
        f"Volume cache populated with {len(code_files)} code files",
        extra={"step": "brownfield_exploration"},
    )

    # ─── Step 5: Run ReAct agent loop with multimodal context ─────────
    try:
        result, tool_call_count, total_input_tokens, total_output_tokens = await _run_react_loop(
            project_id=project_id,
            user_prompt=state.get("prompt", "Analyze this prototype codebase."),
            image_files=image_files,
            pdf_texts=pdf_texts,
            has_code_files=len(code_files) > 0,
        )
    except Exception as e:
        error_msg = f"Brownfield exploration agent failed: {str(e)[:200]}"
        logger.exception(error_msg)
        _log_error_safe(
            project_id=project_id,
            error_code="EXPLORATION_AGENT_ERROR",
            severity="critical",
            source_component="brownfield_exploration_node",
            source_step="brownfield_exploration",
            error_message=error_msg,
            stack_trace=traceback.format_exc(),
        )
        return {
            "error": error_msg,
            "current_step": "brownfield_exploration",
            "completed_steps": [],
        }

    # ─── Step 4: Extract structured results ───────────────────────────
    entities = result.get("entities", [])
    relationships = result.get("relationships", [])
    project_structure = result.get("project_structure", {})
    tech_stack = result.get("tech_stack", {})

    # Derive project name from tech stack or structure if available
    project_name = state.get("project_name", "")
    if not project_name:
        project_name = _derive_project_name(project_structure, tech_stack)

    logger.info(
        f"Brownfield exploration complete: {len(entities)} entities, "
        f"{len(relationships)} relationships, {tool_call_count} tool calls",
        extra={
            "step": "brownfield_exploration",
            "project_name": project_name,
            "tool_call_count": tool_call_count,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        },
    )

    # ─── Step 6: Build prototype_context from exploration results ──────
    # This ensures image-only uploads (no code files) still produce a
    # rich prototype_context for downstream frontend/backend generation.
    prototype_context = _build_prototype_context_from_exploration(result, image_files)

    # ─── Step 7: Extract theme from exploration results ───────────────
    theme = result.get("theme", {})
    if not theme or not theme.get("mode"):
        # Default to light if we have images (most uploaded UI screenshots are light)
        theme = {"mode": "light", "brand_color": "#3b82f6", "brand_name": "blue"}

    return {
        "project_name": project_name,
        "entities": entities,
        "relationships": relationships,
        "project_structure": project_structure,
        "tech_stack": tech_stack,
        "prototype_context": prototype_context,
        "theme": theme,
        "tool_call_count": tool_call_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "current_step": "brownfield_exploration",
        "completed_steps": ["brownfield_exploration"],
    }


# ═══════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


async def _extract_volume_files(volume_paths: list[str]) -> dict[str, bytes]:
    """Download and extract files from Databricks Volume paths.

    Handles zip files by extracting their contents in-memory.
    Non-zip text files are read directly.

    Args:
        volume_paths: List of volume file paths from the upload step.

    Returns:
        Dict mapping relative file paths to raw bytes content.
    """
    workspace_client = get_workspace_client()
    all_files: dict[str, bytes] = {}

    for path in volume_paths:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

        if ext == "zip":
            # Download and extract zip
            response = await asyncio.to_thread(workspace_client.files.download, path)
            zip_bytes = response.contents.read()

            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # Skip __MACOSX and hidden files from zip
                    if info.filename.startswith("__MACOSX") or "/." in info.filename:
                        continue
                    try:
                        all_files[info.filename] = zf.read(info.filename)
                    except Exception as e:
                        logger.debug(f"Skipping unreadable zip entry: {info.filename}: {e}")
        else:
            # Single file — download directly
            try:
                response = await asyncio.to_thread(workspace_client.files.download, path)
                content = response.contents.read()
                # Use just the filename as the key
                filename = path.rsplit("/", 1)[-1] if "/" in path else path
                all_files[filename] = content
            except Exception as e:
                logger.warning(f"Failed to read file {path}: {e}")

    return all_files


async def _run_react_loop(
    project_id: str,
    user_prompt: str,
    image_files: dict[str, bytes] | None = None,
    pdf_texts: dict[str, str] | None = None,
    has_code_files: bool = True,
) -> tuple[dict, int, int, int]:
    """Run the ReAct agent loop with tool calling and multimodal inputs.

    The LLM decides which tool to call, the tool executes, and the result
    is fed back to the LLM. This continues until the LLM responds without
    tool calls (indicating it has finished exploration) or the iteration
    limit is reached.

    Supports multimodal inputs:
    - Images are sent as base64-encoded content blocks in the initial message
    - PDF text is included as context in the initial message
    - Code files are explored via tools

    Args:
        project_id: Project ID for logging.
        user_prompt: User's description of the prototype.
        image_files: Dict of filename → image bytes (PNG/JPG).
        pdf_texts: Dict of filename → extracted text from PDFs.
        has_code_files: Whether code files are available for tool exploration.

    Returns:
        Tuple of (parsed_result_dict, tool_call_count, total_input_tokens, total_output_tokens).
    """
    # Build the tool-calling LLM
    llm = get_llm(max_tokens=16384)

    # Only bind tools if there are code files to explore
    if has_code_files:
        llm_with_tools = llm.bind_tools(BROWNFIELD_TOOLS)
    else:
        llm_with_tools = llm  # No tools — direct analysis from images/PDFs

    # Build tool lookup for execution
    tool_map = {tool.name: tool for tool in BROWNFIELD_TOOLS}

    # ─── Build multimodal initial message ─────────────────────────────
    message_content = _build_multimodal_message(
        user_prompt=user_prompt,
        image_files=image_files or {},
        pdf_texts=pdf_texts or {},
        has_code_files=has_code_files,
    )

    # Initialize messages
    messages: list = [
        SystemMessage(content=BROWNFIELD_EXPLORATION_SYSTEM_PROMPT),
        HumanMessage(content=message_content),
    ]

    tool_call_count = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for iteration in range(MAX_TOOL_CALL_ITERATIONS):
        # Call LLM with tools
        response = invoke_with_logging(
            llm=llm_with_tools,
            messages=messages,
            project_id=project_id,
            call_type="exploration",
        )

        # Track token usage
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata and isinstance(usage_metadata, dict):
            total_input_tokens += usage_metadata.get("input_tokens", 0)
            total_output_tokens += usage_metadata.get("output_tokens", 0)

        # Add AI response to messages
        messages.append(response)

        # Check if LLM wants to call tools
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            # No more tool calls — LLM has finished exploration
            break

        # Execute each tool call and feed results back
        for tool_call in tool_calls:
            tool_call_count += 1
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_call_id = tool_call.get("id", f"call_{tool_call_count}")

            logger.debug(
                f"Tool call #{tool_call_count}: {tool_name}({tool_args})",
                extra={"step": "brownfield_exploration", "tool": tool_name},
            )

            # Execute the tool
            try:
                tool_fn = tool_map.get(tool_name)
                if tool_fn is None:
                    tool_result = json.dumps({"error": f"Unknown tool: {tool_name}"})
                else:
                    tool_result = tool_fn.invoke(tool_args)
            except Exception as e:
                tool_result = json.dumps({"error": f"Tool execution failed: {str(e)[:200]}"})
                logger.warning(
                    f"Tool {tool_name} failed: {e}",
                    extra={"step": "brownfield_exploration", "tool": tool_name},
                )

            # Add tool result as ToolMessage
            messages.append(
                ToolMessage(content=tool_result, tool_call_id=tool_call_id)
            )

    else:
        # Hit iteration limit — ask LLM to produce final output
        logger.warning(
            f"Brownfield exploration hit max iterations ({MAX_TOOL_CALL_ITERATIONS})",
            extra={"step": "brownfield_exploration", "tool_call_count": tool_call_count},
        )
        messages.append(
            HumanMessage(
                content=(
                    "You have reached the maximum number of tool calls. "
                    "Please produce your final structured JSON output now based on what you have learned so far."
                )
            )
        )
        response = invoke_with_logging(
            llm=llm,  # Use LLM without tools for final output
            messages=messages,
            project_id=project_id,
            call_type="exploration_final",
        )
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata and isinstance(usage_metadata, dict):
            total_input_tokens += usage_metadata.get("input_tokens", 0)
            total_output_tokens += usage_metadata.get("output_tokens", 0)

    # Parse the final response
    final_content = response.content if hasattr(response, "content") else str(response)
    try:
        result = parse_llm_json(final_content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse exploration output, attempting retry: {e}")
        _log_error_safe(
            project_id=project_id,
            error_code="LLM_PARSE_ERROR",
            severity="medium",
            source_component="brownfield_exploration_node",
            source_step="brownfield_exploration",
            error_message=f"Failed to parse exploration JSON output: {str(e)[:200]}",
        )
        # Retry with explicit instruction to produce JSON
        messages.append(
            HumanMessage(
                content=(
                    "Your previous response was not valid JSON. "
                    "Please respond with ONLY a valid JSON object matching the required output format. "
                    "No markdown fences, no explanation — just the JSON."
                )
            )
        )
        response = invoke_with_logging(
            llm=llm,
            messages=messages,
            project_id=project_id,
            call_type="exploration_retry",
        )
        usage_metadata = getattr(response, "usage_metadata", None)
        if usage_metadata and isinstance(usage_metadata, dict):
            total_input_tokens += usage_metadata.get("input_tokens", 0)
            total_output_tokens += usage_metadata.get("output_tokens", 0)

        final_content = response.content if hasattr(response, "content") else str(response)
        try:
            result = parse_llm_json(final_content)
        except (json.JSONDecodeError, ValueError) as e2:
            logger.error(f"Exploration output parse failed after retry: {e2}")
            _log_error_safe(
                project_id=project_id,
                error_code="LLM_PARSE_ERROR",
                severity="high",
                source_component="brownfield_exploration_node",
                source_step="brownfield_exploration",
                error_message=f"Failed to parse exploration JSON after retry: {str(e2)[:200]}",
            )
            # Return a minimal result rather than failing completely
            result = {
                "project_structure": {},
                "tech_stack": {},
                "entities": [],
                "relationships": [],
                "api_endpoints": [],
                "ui_structure": {},
                "business_logic": [],
                "production_gaps": [],
                "seed_data_locations": [],
            }

    return result, tool_call_count, total_input_tokens, total_output_tokens


# ═══════════════════════════════════════════════════════════════════════
# FILE CLASSIFICATION & MULTIMODAL HELPERS
# ═══════════════════════════════════════════════════════════════════════

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PDF_EXTENSIONS = {".pdf"}


def _classify_files(all_files: dict[str, bytes]) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, bytes]]:
    """Classify extracted files into code, images, and PDFs.

    Args:
        all_files: Dict mapping filename → raw bytes.

    Returns:
        Tuple of (code_files, image_files, pdf_files) dicts.
    """
    code_files: dict[str, bytes] = {}
    image_files: dict[str, bytes] = {}
    pdf_files: dict[str, bytes] = {}

    for filename, content in all_files.items():
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

        if ext in IMAGE_EXTENSIONS:
            image_files[filename] = content
        elif ext in PDF_EXTENSIONS:
            pdf_files[filename] = content
        else:
            code_files[filename] = content

    return code_files, image_files, pdf_files


def _extract_pdf_texts(pdf_files: dict[str, bytes]) -> dict[str, str]:
    """Extract text content from PDF files.

    Uses a simple approach: tries PyPDF2/pypdf if available,
    falls back to basic binary-to-text extraction.

    Args:
        pdf_files: Dict mapping filename → PDF bytes.

    Returns:
        Dict mapping filename → extracted text.
    """
    texts: dict[str, str] = {}

    for filename, content in pdf_files.items():
        try:
            # Try pypdf (modern PyPDF2 replacement)
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            pages_text = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text)
            if pages_text:
                texts[filename] = "\n\n".join(pages_text)
                logger.info(f"Extracted {len(pages_text)} pages from PDF: {filename}")
            else:
                texts[filename] = f"[PDF file '{filename}' — no extractable text, may contain images only]"
        except ImportError:
            # pypdf not installed — include as placeholder
            texts[filename] = f"[PDF file '{filename}' — text extraction unavailable, install pypdf for PDF support]"
            logger.warning(f"pypdf not installed, cannot extract text from: {filename}")
        except Exception as e:
            texts[filename] = f"[PDF file '{filename}' — extraction failed: {str(e)[:100]}]"
            logger.warning(f"Failed to extract PDF text from {filename}: {e}")

    return texts


def _build_multimodal_message(
    user_prompt: str,
    image_files: dict[str, bytes],
    pdf_texts: dict[str, str],
    has_code_files: bool,
) -> list[dict]:
    """Build a multimodal message content list for Claude.

    LangChain's HumanMessage accepts a list of content blocks:
    - {"type": "text", "text": "..."} for text
    - {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}} for images

    Args:
        user_prompt: User's description.
        image_files: Dict of filename → image bytes.
        pdf_texts: Dict of filename → extracted PDF text.
        has_code_files: Whether code files are available for tool exploration.

    Returns:
        List of content blocks for HumanMessage.
    """
    import base64

    content_blocks: list[dict] = []

    # 1. Text introduction
    intro_parts = [f"## User Description\n{user_prompt}\n"]

    # 2. Add PDF context if available
    if pdf_texts:
        intro_parts.append("\n## PDF Documents Provided\n")
        for filename, text in pdf_texts.items():
            intro_parts.append(f"### {filename}\n{text[:8000]}\n")  # Cap at 8K chars per PDF

    # 3. Instructions based on what's available
    intro_parts.append("\n## Instructions\n")
    if has_code_files:
        intro_parts.append(
            "Code files are available for exploration using the provided tools. "
            "Start by listing the root directory to understand the project structure. "
            "Then systematically explore the codebase using the available tools.\n"
        )
    if image_files:
        intro_parts.append(
            f"\n{len(image_files)} UI screenshot(s) are attached below. "
            "Analyze them to understand the application's visual layout, navigation, "
            "pages, data tables, forms, and UI components. Use this visual context "
            "to inform your entity extraction and UI structure analysis.\n"
        )
    if not has_code_files and not image_files:
        intro_parts.append(
            "Analyze the provided context and produce your structured JSON output.\n"
        )

    intro_parts.append(
        "\nWhen you have gathered enough information, produce your final structured JSON output."
    )

    content_blocks.append({"type": "text", "text": "\n".join(intro_parts)})

    # 4. Add images as vision content blocks
    for filename, img_bytes in image_files.items():
        # Determine MIME type
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/png")

        # Encode as base64 data URL
        b64_data = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64_data}"

        # Add image label
        content_blocks.append({"type": "text", "text": f"\n### UI Screenshot: {filename}"})
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })

    return content_blocks


def _build_prototype_context_from_exploration(result: dict, image_files: dict[str, bytes] | None = None) -> str:
    """Build prototype_context from brownfield exploration results.

    This ensures that when images are uploaded (no code files), the UI analysis
    from the vision model is captured as prototype_context for downstream nodes
    (frontend_dev, backend_dev) to use.

    Args:
        result: Parsed JSON output from the exploration agent.
        image_files: Dict of image filenames (used to determine if this was image-based).

    Returns:
        A rich prototype_context string describing the UI and features.
    """
    parts = []

    # UI Structure (critical for frontend_dev — extracted from image analysis)
    ui_structure = result.get("ui_structure", {})
    if ui_structure:
        parts.append("## UI Structure")
        if ui_structure.get("pages"):
            parts.append(f"Pages/Views: {', '.join(ui_structure['pages'])}")
        if ui_structure.get("components"):
            parts.append(f"Key Components: {', '.join(ui_structure['components'])}")
        if ui_structure.get("navigation"):
            parts.append(f"Navigation: {ui_structure['navigation']}")
        if ui_structure.get("data_sources"):
            parts.append(f"Data Sources: {json.dumps(ui_structure['data_sources'])}")
        # Include any additional UI details the LLM may have provided
        for key, value in ui_structure.items():
            if key not in ("pages", "components", "navigation", "data_sources") and value:
                parts.append(f"{key.replace('_', ' ').title()}: {value}")
        parts.append("")

    # API Endpoints (for backend_dev)
    api_endpoints = result.get("api_endpoints", [])
    if api_endpoints:
        parts.append("## API Endpoints")
        for ep in api_endpoints:
            method = ep.get("method", "GET")
            path = ep.get("path", "/")
            desc = ep.get("description", "")
            parts.append(f"- {method} {path} — {desc}")
            if ep.get("request_body"):
                parts.append(f"  Request: {ep['request_body']}")
            if ep.get("response_shape"):
                parts.append(f"  Response: {ep['response_shape']}")
        parts.append("")

    # Business Logic
    business_logic = result.get("business_logic", [])
    if business_logic:
        parts.append("## Business Logic")
        if isinstance(business_logic, list):
            for rule in business_logic:
                name = rule.get("name", "")
                desc = rule.get("description", "")
                parts.append(f"- {name}: {desc}")
        elif isinstance(business_logic, str):
            parts.append(business_logic)
        parts.append("")

    # Tech Stack
    tech_stack = result.get("tech_stack", {})
    if tech_stack:
        parts.append("## Original Tech Stack")
        if isinstance(tech_stack, dict):
            for key, value in tech_stack.items():
                if value:
                    parts.append(f"- {key}: {value}")
        elif isinstance(tech_stack, str):
            parts.append(tech_stack)
        parts.append("")

    # Production Gaps
    production_gaps = result.get("production_gaps", [])
    if production_gaps:
        parts.append("## Production Gaps to Address")
        for gap in production_gaps:
            severity = gap.get("severity", "medium")
            category = gap.get("category", "")
            desc = gap.get("description", "")
            parts.append(f"- [{severity}] {category}: {desc}")
        parts.append("")

    # If this was an image-only upload, add a note for downstream nodes
    if image_files and not result.get("project_structure", {}).get("root_files"):
        parts.insert(0, "## Source: UI Screenshot Analysis\n"
                       "This prototype context was derived from analyzing uploaded UI screenshots. "
                       "The frontend should replicate the visual design shown in the screenshots.\n")

    return "\n".join(parts)


def _derive_project_name(project_structure: dict, tech_stack: dict) -> str:
    """Derive a kebab-case project name from the analysis results.

    Falls back to 'unnamed-prototype' if no meaningful name can be derived.
    """
    # Try to get from project structure entrypoint
    entrypoint = project_structure.get("entrypoint", "")
    if entrypoint:
        # Use the directory name or a meaningful part
        parts = entrypoint.replace("\\", "/").split("/")
        if len(parts) > 1:
            name = parts[0].lower().replace("_", "-").replace(" ", "-")
            if name and name not in ("src", "app", "lib", "main"):
                return name

    # Try framework name
    framework = tech_stack.get("framework", "")
    language = tech_stack.get("language", "")
    if framework:
        return f"{framework.lower().replace(' ', '-')}-prototype"
    if language:
        return f"{language.lower()}-prototype"

    return "unnamed-prototype"


def _log_error_safe(
    project_id: str,
    error_code: str,
    severity: str,
    source_component: str,
    source_step: str,
    error_message: str,
    stack_trace: str | None = None,
    error_context: str | None = None,
) -> None:
    """Log an error via ErrorLoggingService (fire-and-forget).

    Never raises — falls back to structured logger on any failure.
    """
    try:
        error_service = get_error_logging_service()
        error_service.log_error(
            project_id=project_id,
            error_code=error_code,
            severity=severity,
            source_component=source_component,
            source_step=source_step,
            error_message=error_message,
            stack_trace=stack_trace,
            error_context=error_context,
            is_retryable=False,
        )
    except Exception as e:
        logger.warning(
            f"Failed to log error to DB (fire-and-forget): {e}",
            extra={
                "project_id": project_id,
                "error_code": error_code,
                "severity": severity,
                "source_component": source_component,
            },
        )
