"""Intake Agent — Node 1.

Classifies whether the prompt has sufficient information to proceed.
Extracts entities, relationships, and project name.
If insufficient, returns clarification questions.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger

INTAKE_SYSTEM_PROMPT = """You are a business analyst. Analyze the user's prompt and extract application requirements.

You MUST return valid JSON with this exact structure:
{
  "is_sufficient": true/false,
  "clarification_questions": ["question1", "question2"],
  "project_name": "kebab-case-name",
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
- Every entity MUST have: id (UUID PK), created_at (TIMESTAMPTZ), updated_at (TIMESTAMPTZ)
- Use PostgreSQL types: UUID, TEXT, VARCHAR(n), INTEGER, BOOLEAN, TIMESTAMPTZ, JSONB, NUMERIC
- project_name must be kebab-case
- Entity names are singular snake_case
- Return ONLY the JSON, no markdown, no explanation
"""


def intake_node(state: PipelineState) -> dict:
    """Analyze the user prompt and extract structured requirements."""
    logger.info("Node: intake_agent — analyzing prompt", extra={"step": "intake"})

    llm = get_llm(max_tokens=4096)

    response = llm.invoke(
        [
            SystemMessage(content=INTAKE_SYSTEM_PROMPT),
            HumanMessage(content=f"Analyze this prompt and extract entities:\n\n{state['prompt']}"),
        ]
    )

    # Parse the JSON response
    try:
        result = _parse_json(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Intake agent returned invalid JSON, retrying: {e}")
        # Retry once
        response = llm.invoke(
            [
                SystemMessage(content=INTAKE_SYSTEM_PROMPT),
                HumanMessage(
                    content=f"Analyze this prompt and extract entities. Return ONLY valid JSON:\n\n{state['prompt']}"
                ),
            ]
        )
        result = _parse_json(response.content)

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
        "current_step": "intake",
        "completed_steps": ["intake"],
    }


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
