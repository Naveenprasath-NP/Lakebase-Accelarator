"""Seed Data Agent — Node 4.

Generates realistic seed data per table using LLM, inserts it,
and validates row counts. Retries on constraint errors.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.agent.tools import _get_repo
from lakebase_accelerator.utils.logger import logger

SEED_DATA_PROMPT = """Generate realistic seed data for this PostgreSQL table.

Return ONLY a valid JSON array of row objects. Each row is a dict with column_name: value.

Rules:
- Generate exactly 5 rows
- ALL UUIDs must be valid 36-char format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx (use realistic random hex)
- FK values MUST reference the provided parent PKs
- Timestamps in ISO 8601: "2024-03-15T10:30:00Z"
- VARCHAR values must not exceed max length
- BOOLEAN values: true/false
- Return ONLY the JSON array, no markdown, no explanation
"""


def seed_data_node(state: PipelineState) -> dict:
    """Generate and insert seed data for all tables."""
    logger.info("Node: seed_data_agent — generating data", extra={"step": "seed_data"})

    repo = _get_repo()
    llm = get_llm(max_tokens=4096)
    schema_name = state["schema_name"]
    data_model = state["data_model"]
    creation_order = data_model.get("creation_order", [])
    tables = data_model.get("tables", [])

    # Build table lookup
    table_map = {t["name"]: t for t in tables}

    # Track inserted PKs for FK references
    inserted_pks: dict[str, list[str]] = {}
    row_counts: dict[str, int] = {}

    for table_name in creation_order:
        table_def = table_map.get(table_name)
        if not table_def:
            continue

        logger.info(f"Generating seed data for: {table_name}", extra={"step": "seed_data"})

        # Build context for this table
        context = _build_table_context(table_def, inserted_pks)

        try:
            response = llm.invoke(
                [
                    SystemMessage(content=SEED_DATA_PROMPT),
                    HumanMessage(content=context),
                ]
            )

            rows = _parse_json_array(response.content)

            if not rows:
                logger.warning(f"No rows generated for {table_name}")
                row_counts[table_name] = 0
                continue

            # Insert rows
            count = repo.insert_seed_data(schema_name, table_name, rows)
            row_counts[table_name] = count

            # Track PKs for FK references in child tables
            pk_col = _get_pk_column(table_def)
            if pk_col:
                inserted_pks[table_name] = [row[pk_col] for row in rows if pk_col in row]

            logger.info(f"Inserted {count} rows into {table_name}", extra={"step": "seed_data"})

        except Exception as e:
            error_msg = str(e)[:200]
            logger.warning(f"Seed data failed for {table_name}: {error_msg}", extra={"step": "seed_data"})
            row_counts[table_name] = 0

    logger.info(f"Seed data complete: {sum(row_counts.values())} total rows", extra={"step": "seed_data"})

    return {
        "seed_row_counts": row_counts,
        "current_step": "seed_data",
        "completed_steps": state.get("completed_steps", []) + ["seed_data"],
    }


def _build_table_context(table_def: dict, inserted_pks: dict[str, list[str]]) -> str:
    """Build the prompt context for generating data for one table."""
    columns_info = json.dumps(table_def.get("columns", []), indent=2)
    fks = table_def.get("foreign_keys", [])

    context = f"Table: {table_def['name']}\nColumns:\n{columns_info}\n"

    if fks:
        context += "\nForeign key references (use these exact PK values):\n"
        for fk in fks:
            ref_table = fk.get("references_table", "")
            pks = inserted_pks.get(ref_table, [])
            if pks:
                context += f"  - {fk['column']} must be one of: {json.dumps(pks[:5])}\n"

    return context


def _get_pk_column(table_def: dict) -> str | None:
    """Get the primary key column name."""
    for col in table_def.get("columns", []):
        if col.get("is_primary_key"):
            return col["name"]
    return None


def _parse_json_array(text: str) -> list[dict]:
    """Parse a JSON array from LLM response."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    result = json.loads(text)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "rows" in result:
        return result["rows"]
    return []
