"""Data Model Agent — Node 2.

Converts extracted entities into a DDL-ready PostgreSQL data model
with tables, columns, foreign keys, indexes, and creation order.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger

DATA_MODEL_SYSTEM_PROMPT = """You are a PostgreSQL database architect. Convert entities into a production-ready data model.

Return ONLY valid JSON with this structure:
{
  "tables": [
    {
      "name": "table_name_plural",
      "columns": [
        {"name": "id", "data_type": "UUID", "nullable": false, "is_primary_key": true, "default_expression": "gen_random_uuid()"},
        {"name": "col", "data_type": "VARCHAR(255)", "nullable": true, "is_primary_key": false, "default_expression": null}
      ],
      "foreign_keys": [
        {"column": "user_id", "references_table": "users", "references_column": "id", "on_delete": "CASCADE"}
      ],
      "indexes": [
        {"name": "idx_table_col", "table_name": "table_name", "columns": ["col"], "unique": false}
      ]
    }
  ],
  "creation_order": ["parent_table", "child_table"]
}

Rules:
- Table names are PLURAL snake_case (entity "user" → table "users")
- Every table MUST have: id UUID PK, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
- creation_order: parent tables (no FKs) first, then children
- Add indexes on all FK columns and commonly filtered columns (status, email)
- Index names: idx_{table}_{column}
- No circular FK references
- Return ONLY JSON, no markdown, no explanation
"""


def data_model_node(state: PipelineState) -> dict:
    """Design the PostgreSQL data model from extracted entities."""
    logger.info("Node: data_model_agent — designing schema", extra={"step": "data_model"})

    llm = get_llm(max_tokens=8192)

    entities_json = json.dumps(
        {
            "entities": state["entities"],
            "relationships": state["relationships"],
        },
        indent=2,
    )

    response = llm.invoke(
        [
            SystemMessage(content=DATA_MODEL_SYSTEM_PROMPT),
            HumanMessage(content=f"Design a PostgreSQL data model for these entities:\n\n{entities_json}"),
        ]
    )

    try:
        data_model = _parse_json(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Data model agent returned invalid JSON, retrying: {e}")
        response = llm.invoke(
            [
                SystemMessage(content=DATA_MODEL_SYSTEM_PROMPT),
                HumanMessage(content=f"Design a PostgreSQL data model. Return ONLY valid JSON:\n\n{entities_json}"),
            ]
        )
        data_model = _parse_json(response.content)

    # Validate basic structure
    tables = data_model.get("tables", [])
    creation_order = data_model.get("creation_order", [t["name"] for t in tables])

    logger.info(
        f"Data model complete: {len(tables)} tables, order: {creation_order}",
        extra={"step": "data_model"},
    )

    return {
        "data_model": data_model,
        "current_step": "data_model",
        "completed_steps": state.get("completed_steps", []) + ["data_model"],
    }


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())
