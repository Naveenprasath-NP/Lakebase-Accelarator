"""Prototype Analysis Node — Brownfield Step 2 (LLM Call 2).

Runs AFTER prototype_ingestion (which extracted the data model).
This node focuses on:
- Extracting actual seed data from the prototype code
- Describing the UI structure and features in detail
- Identifying API endpoints and business logic
- Providing context for backend and frontend generation

The output (prototype_context + extracted_seed_data) flows to:
- seed_data node → uses extracted_seed_data instead of generating mock
- backend_dev node → uses prototype_context for endpoint design
- frontend_dev node → uses prototype_context for UI replication
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.services.dependencies import get_workspace_client
from lakebase_accelerator.services.volume_reader_service import VolumeReaderService
from lakebase_accelerator.utils.logger import logger

PROTOTYPE_ANALYSIS_PROMPT = """You are a senior full-stack developer analyzing a prototype application.

The data model has already been extracted (entities listed below). Now your job is to extract:

1. **SEED DATA**: Find ALL hardcoded data, mock data, sample data, initial values, or test fixtures in the code.
   - Look for: arrays of objects, JSON data, SQL INSERT statements, factory functions, seed scripts, constant arrays
   - Extract the ACTUAL values — these will be inserted into the production database
   - Map the data to the entity names provided below

2. **UI DESCRIPTION**: Describe the prototype's frontend in FULL detail:
   - What pages/views exist
   - Layout structure (sidebar, header, main content, etc.)
   - What each page shows (tables, forms, charts, dashboards, cards, etc.)
   - Navigation structure
   - Interactive features (filters, search, modals, drag-and-drop, etc.)
   - Styling approach (colors, theme, component library used)
   - Any charts/visualizations and what data they show

3. **API ENDPOINTS**: List ALL API endpoints the prototype implements:
   - HTTP method + path
   - What it does (business logic, not just CRUD)
   - Request body shape
   - Response shape
   - Any special logic (filtering, aggregation, calculations)

4. **BUSINESS LOGIC**: Key rules, calculations, workflows:
   - Validation rules beyond basic type checking
   - Computed fields or derived data
   - State machines or workflow transitions
   - Aggregations or reporting logic

Return ONLY valid JSON:
{
  "seed_data": {
    "entity_name": [
      {"column_name": "actual_value_from_code", "another_column": "actual_value"}
    ]
  },
  "ui_description": "DETAILED description of the UI. Be specific: pages, layout, components, interactions, colors, charts. A developer should be able to recreate the UI from this description alone.",
  "api_endpoints": [
    {
      "method": "GET|POST|PUT|DELETE",
      "path": "/api/resource",
      "description": "what it does including any business logic",
      "request_body": "description of request shape or null",
      "response_shape": "description of response shape"
    }
  ],
  "business_logic": "Detailed description of business rules, calculations, validations, workflows found in the code.",
  "tech_stack": "Frontend: React/Vue/Angular/HTML. Backend: Flask/Express/FastAPI. DB: SQLite/Postgres/etc.",
  "production_gaps": [
    {"category": "backend|validation|auth|error_handling|database|security|performance", "description": "...", "severity": "critical|high|medium|low"}
  ]
}
"""


async def prototype_analysis_node(state: PipelineState) -> dict:
    """Extract seed data, UI context, and API structure from the prototype.

    LLM Call 2 of 2 for brownfield:
    - Re-reads files from volume (focused on data + UI + API)
    - Uses entities from step 1 as context
    - Outputs prototype_context and extracted_seed_data for downstream nodes
    """
    logger.info(
        "Node: prototype_analysis — extracting seed data and UI context",
        extra={"step": "prototype_analysis"},
    )

    volume_paths = state.get("volume_paths", [])
    entities = state.get("entities", [])

    # 1. Re-read files from volume
    try:
        workspace_client = get_workspace_client()
        volume_reader = VolumeReaderService(workspace_client)
        file_contents = await volume_reader.read_prototype_files(volume_paths)
    except Exception as e:
        logger.warning(f"Failed to re-read files for analysis: {e}")
        # Non-fatal — continue with empty context
        return {
            "prototype_context": "",
            "extracted_seed_data": {},
            "current_step": "prototype_analysis",
            "completed_steps": state.get("completed_steps", []) + ["prototype_analysis"],
        }

    # 2. Build entities summary for context
    entities_summary = _format_entities(entities)

    # 3. Build user prompt
    user_prompt = _build_analysis_prompt(state["prompt"], file_contents, entities_summary)

    # 4. Call LLM — focused on seed data + UI + API extraction
    llm = get_llm(max_tokens=16384)

    response = llm.invoke(
        [
            SystemMessage(content=PROTOTYPE_ANALYSIS_PROMPT),
            HumanMessage(content=user_prompt),
        ]
    )

    # 5. Parse response
    try:
        result = _parse_json(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Prototype analysis returned invalid JSON, retrying: {e}")
        response = llm.invoke(
            [
                SystemMessage(content=PROTOTYPE_ANALYSIS_PROMPT),
                HumanMessage(content=f"Return ONLY valid JSON. No markdown.\n\n{user_prompt}"),
            ]
        )
        try:
            result = _parse_json(response.content)
        except (json.JSONDecodeError, ValueError):
            logger.error("Prototype analysis failed to return valid JSON after retry")
            result = {}

    # 6. Build prototype context for downstream nodes
    seed_data = result.get("seed_data", {})
    prototype_context = _build_prototype_context(result)

    logger.info(
        f"Prototype analysis complete: seed_data_tables={len(seed_data)}, "
        f"endpoints={len(result.get('api_endpoints', []))}, "
        f"context_length={len(prototype_context)} chars",
        extra={"step": "prototype_analysis"},
    )

    return {
        "prototype_context": prototype_context,
        "extracted_seed_data": seed_data,
        "current_step": "prototype_analysis",
        "completed_steps": state.get("completed_steps", []) + ["prototype_analysis"],
    }


def _format_entities(entities: list[dict]) -> str:
    """Format entities list for the LLM context."""
    parts = []
    for entity in entities:
        attrs = entity.get("attributes", [])
        attr_names = [a["name"] for a in attrs if a["name"] not in ("id", "created_at", "updated_at")]
        parts.append(f"- {entity['name']}: {', '.join(attr_names)}")
    return "\n".join(parts)


def _build_analysis_prompt(prompt: str, file_contents: dict[str, str], entities_summary: str) -> str:
    """Build the user prompt for the analysis LLM call."""
    parts = [
        f"## User Description\n{prompt}\n",
        f"\n## Extracted Entities (from previous step)\n{entities_summary}\n",
        f"\n## Prototype Source Files ({len(file_contents)} files)\n",
        "Extract SEED DATA, UI DESCRIPTION, and API ENDPOINTS from these files:\n",
    ]

    for filepath, content in file_contents.items():
        ext = filepath.rsplit(".", 1)[-1] if "." in filepath else ""
        parts.append(f"\n### {filepath}\n```{ext}\n{content}\n```\n")

    return "\n".join(parts)


def _build_prototype_context(result: dict) -> str:
    """Build the prototype_context string for downstream generation nodes."""
    parts = []

    # UI Description (critical for frontend_dev)
    if result.get("ui_description"):
        parts.append(f"## UI Description\n{result['ui_description']}\n")

    # API Endpoints (critical for backend_dev)
    if result.get("api_endpoints"):
        parts.append("## API Endpoints")
        for ep in result["api_endpoints"]:
            method = ep.get("method", "GET")
            path = ep.get("path", "/")
            desc = ep.get("description", "")
            parts.append(f"- {method} {path} — {desc}")
            if ep.get("request_body"):
                parts.append(f"  Request: {ep['request_body']}")
            if ep.get("response_shape"):
                parts.append(f"  Response: {ep['response_shape']}")
        parts.append("")

    # Business Logic (for backend_dev)
    if result.get("business_logic"):
        parts.append(f"## Business Logic\n{result['business_logic']}\n")

    # Tech Stack
    if result.get("tech_stack"):
        parts.append(f"## Original Tech Stack\n{result['tech_stack']}\n")

    # Production Gaps
    if result.get("production_gaps"):
        parts.append("## Production Gaps to Address")
        for gap in result["production_gaps"]:
            severity = gap.get("severity", "medium")
            category = gap.get("category", "")
            desc = gap.get("description", "")
            parts.append(f"- [{severity}] {category}: {desc}")
        parts.append("")

    return "\n".join(parts)


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown wrappers."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())
