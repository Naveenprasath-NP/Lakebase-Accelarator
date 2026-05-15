"""Intake Agent — Node 1.

Classifies whether the prompt has sufficient information to proceed.
Extracts entities, relationships, and project name.
If insufficient, returns clarification questions.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.json_repair import parse_llm_json
from lakebase_accelerator.utils.logger import logger

INTAKE_SYSTEM_PROMPT = """You are a business analyst. Analyze the user's prompt and extract application requirements.

You MUST return valid JSON with this exact structure:
{
  "is_sufficient": true/false,
  "clarification_questions": [],
  "project_name": "kebab-case-name",
  "theme": {"mode": "dark", "brand_color": "#3b82f6", "brand_name": "blue"},
  "layout": "sidebar",
  "entities": [
    {
      "name": "entity_name_singular_snake_case",
      "description": "what this entity represents",
      "attributes": [
        {"name": "id", "data_type": "UUID", "nullable": false, "is_primary_key": true, "default_value": "gen_random_uuid()"},
        {"name": "column_name", "data_type": "VARCHAR(255)", "nullable": true, "is_primary_key": false, "default_value": null}
      ]
    }
  ],
  "relationships": [
    {"from_entity": "child", "to_entity": "parent", "cardinality": "many_to_one", "foreign_key_column": "parent_id"}
  ]
}

Rules:
- Set is_sufficient=false ONLY if the prompt is extremely vague (less than 5 words or no discernible domain)
- If is_sufficient=true, clarification_questions MUST be an empty array []
- Every entity MUST have: id (UUID PK), created_at (TIMESTAMPTZ), updated_at (TIMESTAMPTZ)
- Use PostgreSQL types: UUID, TEXT, VARCHAR(n), INTEGER, BOOLEAN, TIMESTAMPTZ, JSONB, NUMERIC
- project_name must be kebab-case
- Entity names are singular snake_case
- Keep entities focused: max 6-8 entities, max 8-10 attributes per entity (excluding id, created_at, updated_at)
- theme.mode: "dark" (default) or "light" — only set to "light" if user explicitly asks for light/white theme
- theme.brand_color: hex color for primary brand — default "#3b82f6" (blue). Change ONLY if user mentions a specific color (e.g., "green theme" → "#22c55e", "red" → "#ef4444", "purple" → "#8b5cf6", "orange" → "#f97316", "yellow" → "#eab308")
- theme.brand_name: human-readable color name (e.g., "blue", "green", "purple")
- layout: choose the best layout for the application type:
  * "sidebar" (default) — for admin panels, CRM, project management, multi-entity apps with 4+ entities
  * "topnav" — for public-facing apps, portfolios, blogs, landing pages, apps with 2-3 entities
  * "minimal" — for single-purpose tools, simple CRUD apps with 1-2 entities, personal utilities
- Return ONLY the JSON, no markdown, no explanation, no trailing text
"""


def intake_node(state: PipelineState) -> dict:
    """Analyze the user prompt and extract structured requirements."""
    logger.info("Node: intake_agent — analyzing prompt", extra={"step": "intake"})

    llm = get_llm(max_tokens=32768)

    response = llm.invoke(
        [
            SystemMessage(content=INTAKE_SYSTEM_PROMPT),
            HumanMessage(content=f"Analyze this prompt and extract entities:\n\n{state['prompt']}"),
        ]
    )

    # Parse the JSON response
    try:
        result = parse_llm_json(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Intake agent returned invalid JSON, retrying: {e}")
        # Retry once with higher token limit
        llm_retry = get_llm(max_tokens=32768)
        response = llm_retry.invoke(
            [
                SystemMessage(content=INTAKE_SYSTEM_PROMPT),
                HumanMessage(
                    content=f"Analyze this prompt and extract entities. Return ONLY valid JSON, keep it concise:\n\n{state['prompt']}"
                ),
            ]
        )
        try:
            result = parse_llm_json(response.content)
        except (json.JSONDecodeError, ValueError) as e2:
            logger.error(f"Intake agent failed after retry: {e2}")
            result = {"is_sufficient": False, "clarification_questions": ["Could you provide more details about your application?"], "entities": [], "relationships": [], "project_name": "unnamed-project"}

    is_sufficient = result.get("is_sufficient", True)
    project_name = result.get("project_name", "unnamed-project")

    # Use provided project_name if available
    if state.get("project_name"):
        project_name = state["project_name"]

    logger.info(
        f"Intake complete: sufficient={is_sufficient}, entities={len(result.get('entities', []))}",
        extra={"step": "intake", "project_name": project_name},
    )

    return {
        "is_sufficient": is_sufficient,
        "clarification_questions": result.get("clarification_questions", []),
        "project_name": project_name,
        "entities": result.get("entities", []),
        "relationships": result.get("relationships", []),
        "theme": result.get("theme", {"mode": "dark", "brand_color": "#3b82f6", "brand_name": "blue"}),
        "layout": result.get("layout", "sidebar"),
        "current_step": "intake",
        "completed_steps": ["intake"],
    }


