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

SEED_DATA_PROMPT = """Generate realistic, demo-quality seed data for this PostgreSQL table.

Return ONLY a valid JSON array of row objects. Each row is a dict with column_name: value.

Rules:
- Generate exactly 10-15 rows (enough to fill a table and show meaningful stats)
- ALL UUIDs must be valid 36-char format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx (use realistic random hex)
- FK values MUST reference the provided parent PKs (distribute evenly across parents)
- Timestamps in ISO 8601: "2024-03-15T10:30:00Z" — use dates within the last 30 days for realism
- VARCHAR values must not exceed max length
- BOOLEAN values: true/false
- If there's a "status" column, distribute values across different statuses (e.g., 4 pending, 3 approved, 2 rejected, 1 completed) — this makes dashboards look interesting
- Use realistic business data: real-sounding names, emails, descriptions, amounts
- Make data diverse — different values, not repetitive patterns
- For amounts/prices: use realistic ranges (e.g., $50-$5000 for orders, $40000-$150000 for salaries)
- For dates: spread across the last 30 days, some today, some this week
- Return ONLY the JSON array, no markdown, no explanation
"""


def seed_data_node(state: PipelineState) -> dict:
    """Generate and insert seed data for all tables.

    Parallelizes LLM calls for tables at the same dependency level.
    Inserts in topological order to respect FK constraints.

    For brownfield: uses extracted_seed_data from prototype when available.
    For greenfield (or missing data): generates mock data via LLM.
    """
    import asyncio
    import concurrent.futures

    logger.info("Node: seed_data_agent — generating data", extra={"step": "seed_data"})

    repo = _get_repo()
    schema_name = state["schema_name"]
    data_model = state["data_model"]
    creation_order = data_model.get("creation_order", [])
    tables = data_model.get("tables", [])

    # Brownfield: check for extracted seed data from prototype
    extracted_seed_data = state.get("extracted_seed_data", {})
    pipeline_type = state.get("pipeline_type", "greenfield")

    if extracted_seed_data and pipeline_type == "brownfield":
        logger.info(
            f"Using extracted seed data from prototype for {len(extracted_seed_data)} tables",
            extra={"step": "seed_data"},
        )

    # Build table lookup and dependency graph
    table_map = {t["name"]: t for t in tables}

    # Group tables by dependency level for parallel generation
    levels = _group_by_dependency_level(tables, creation_order)
    logger.info(f"Seed data: {len(levels)} dependency levels, {len(creation_order)} tables", extra={"step": "seed_data"})

    # Track inserted PKs for FK references
    inserted_pks: dict[str, list[str]] = {}
    row_counts: dict[str, int] = {}
    generated_data: dict[str, list[dict]] = {}

    for level_idx, level_tables in enumerate(levels):
        # Generate data for all tables in this level IN PARALLEL
        tables_needing_generation = []
        for table_name in level_tables:
            extracted_rows = extracted_seed_data.get(table_name, [])
            if extracted_rows and isinstance(extracted_rows, list) and len(extracted_rows) > 0:
                generated_data[table_name] = extracted_rows
                logger.info(f"Using {len(extracted_rows)} extracted rows for {table_name}", extra={"step": "seed_data"})
            else:
                tables_needing_generation.append(table_name)

        if tables_needing_generation:
            logger.info(
                f"Level {level_idx + 1}: generating data for {len(tables_needing_generation)} tables in parallel",
                extra={"step": "seed_data"},
            )

            # Parallel LLM calls for this level
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(tables_needing_generation))) as executor:
                futures = {}
                for table_name in tables_needing_generation:
                    table_def = table_map.get(table_name)
                    if not table_def:
                        continue
                    context = _build_table_context(table_def, inserted_pks)
                    futures[executor.submit(_generate_table_data, context, table_name)] = table_name

                for future in concurrent.futures.as_completed(futures):
                    table_name = futures[future]
                    try:
                        rows = future.result(timeout=120)
                        generated_data[table_name] = rows
                    except Exception as e:
                        logger.warning(f"Parallel seed generation failed for {table_name}: {e}", extra={"step": "seed_data"})
                        generated_data[table_name] = []

        # Insert all tables in this level (sequentially, in creation_order)
        for table_name in level_tables:
            table_def = table_map.get(table_name)
            if not table_def:
                continue

            rows = generated_data.get(table_name, [])
            if not rows:
                logger.warning(f"No rows available for {table_name}")
                row_counts[table_name] = 0
                continue

            try:
                count = repo.insert_seed_data(schema_name, table_name, rows)
                row_counts[table_name] = count

                # Track PKs for FK references in child tables
                pk_col = _get_pk_column(table_def)
                if pk_col:
                    inserted_pks[table_name] = [row[pk_col] for row in rows if pk_col in row]

                logger.info(f"Inserted {count} rows into {table_name}", extra={"step": "seed_data"})

            except Exception as e:
                error_msg = str(e)[:200]
                logger.warning(f"Seed data insert failed for {table_name}: {error_msg}", extra={"step": "seed_data"})
                row_counts[table_name] = 0

    logger.info(f"Seed data complete: {sum(row_counts.values())} total rows", extra={"step": "seed_data"})

    return {
        "seed_row_counts": row_counts,
        "current_step": "seed_data",
        "completed_steps": state.get("completed_steps", []) + ["seed_data"],
    }


def _generate_table_data(context: str, table_name: str) -> list[dict]:
    """Generate seed data for a single table via LLM (called in thread pool)."""
    llm = get_llm(max_tokens=16384)
    try:
        response = llm.invoke(
            [
                SystemMessage(content=SEED_DATA_PROMPT),
                HumanMessage(content=context),
            ]
        )
        return _parse_json_array(response.content)
    except Exception as e:
        logger.warning(f"LLM seed data generation failed for {table_name}: {e}")
        return []


def _group_by_dependency_level(tables: list[dict], creation_order: list[str]) -> list[list[str]]:
    """Group tables into dependency levels for parallel generation.

    Level 0: tables with no FK dependencies (can all generate in parallel)
    Level 1: tables that depend only on level 0 tables
    Level 2: tables that depend on level 0 or 1 tables
    etc.
    """
    # Build dependency map: table -> set of tables it depends on
    deps: dict[str, set[str]] = {}
    for table in tables:
        table_name = table["name"]
        fk_targets = set()
        for fk in table.get("foreign_keys", []):
            ref = fk.get("references_table", "")
            if ref and ref != table_name:  # Exclude self-references
                fk_targets.add(ref)
        deps[table_name] = fk_targets

    # Assign levels
    levels: list[list[str]] = []
    assigned: set[str] = set()

    # Use creation_order to maintain deterministic ordering within levels
    remaining = list(creation_order)

    while remaining:
        # Find tables whose dependencies are all already assigned
        current_level = []
        for table_name in remaining:
            table_deps = deps.get(table_name, set())
            if table_deps.issubset(assigned):
                current_level.append(table_name)

        if not current_level:
            # Circular dependency or missing table — just add remaining
            current_level = remaining[:]

        levels.append(current_level)
        assigned.update(current_level)
        remaining = [t for t in remaining if t not in assigned]

    return levels


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
