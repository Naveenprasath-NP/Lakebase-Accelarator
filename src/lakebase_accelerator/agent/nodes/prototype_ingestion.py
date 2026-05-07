"""Prototype Ingestion Node — Brownfield Step 1 (LLM Call 1).

Focused ONLY on reverse-engineering the data model from the prototype.
Reads uploaded files from the Databricks Volume and extracts:
- Entities with their attributes and types
- Relationships between entities
- Project name

This is the first of two brownfield-specific LLM calls.
The second call (prototype_analysis_node) handles seed data and UI context.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.services.dependencies import get_workspace_client
from lakebase_accelerator.services.volume_reader_service import VolumeReaderService
from lakebase_accelerator.utils.json_repair import parse_llm_json
from lakebase_accelerator.utils.logger import logger

PROTOTYPE_DATA_MODEL_PROMPT = """You are a database architect specializing in reverse engineering.

Analyze the provided prototype source code and extract the DATA MODEL ONLY.
Focus on: database tables, ORM models, SQL schemas, data structures, API response shapes.

Look for:
- SQL CREATE TABLE statements
- ORM model definitions (SQLAlchemy, Prisma, TypeORM, Sequelize, Django models)
- Pydantic/dataclass models that represent database entities
- API response shapes that imply database structure
- Hardcoded arrays/objects that imply entity structure
- JSON/YAML config files with data schemas

Rules:
- Every entity MUST have `id` (UUID PK, default gen_random_uuid()), `created_at` (TIMESTAMPTZ, default NOW()), `updated_at` (TIMESTAMPTZ, default NOW())
- Use snake_case for all entity and attribute names
- Use PostgreSQL types: UUID, TEXT, VARCHAR(n), INTEGER, BIGINT, NUMERIC, BOOLEAN, TIMESTAMPTZ, DATE, JSONB
- Entity names must be singular snake_case
- project_name must be kebab-case
- Identify ALL entities — don't miss any tables/models in the code
- Map relationships correctly (one-to-many, many-to-one, many-to-many)

Return ONLY valid JSON:
{
  "project_name": "kebab-case-name",
  "domain_summary": "1-2 sentence summary",
  "is_sufficient": true,
  "entities": [
    {
      "name": "entity_name",
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
"""


async def prototype_ingestion_node(state: PipelineState) -> dict:
    """Reverse-engineer the data model from uploaded prototype files.

    LLM Call 1 of 2 for brownfield:
    - Reads files from volume
    - Extracts ONLY entities and relationships (data model focus)
    - Downstream: prototype_analysis_node handles seed data + UI context
    """
    logger.info(
        "Node: prototype_ingestion — extracting data model from prototype",
        extra={"step": "prototype_ingestion", "volume_paths": len(state.get("volume_paths", []))},
    )

    volume_paths = state.get("volume_paths", [])
    if not volume_paths:
        logger.error("No volume paths in state for brownfield pipeline")
        return {
            "error": "No files uploaded for brownfield pipeline",
            "current_step": "prototype_ingestion",
            "completed_steps": [],
        }

    # 1. Read and process files from volume
    try:
        workspace_client = get_workspace_client()
        volume_reader = VolumeReaderService(workspace_client)
        file_contents = await volume_reader.read_prototype_files(volume_paths)
    except Exception as e:
        logger.exception(f"Failed to read prototype files from volume: {e}")
        return {
            "error": f"Failed to read uploaded files: {str(e)[:200]}",
            "current_step": "prototype_ingestion",
            "completed_steps": [],
        }

    if not file_contents:
        logger.warning("No readable files found in uploaded prototype")
        return {
            "error": "No readable text files found in the uploaded prototype",
            "current_step": "prototype_ingestion",
            "completed_steps": [],
        }

    logger.info(
        f"Read {len(file_contents)} files, extracting data model via LLM",
        extra={"step": "prototype_ingestion"},
    )

    # 2. Build user prompt with file context
    user_prompt = _build_user_prompt(state["prompt"], file_contents)

    # 3. Call LLM — focused ONLY on data model extraction
    llm = get_llm(max_tokens=16384)

    response = llm.invoke(
        [
            SystemMessage(content=PROTOTYPE_DATA_MODEL_PROMPT),
            HumanMessage(content=user_prompt),
        ]
    )

    # 4. Parse JSON response
    try:
        result = parse_llm_json(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Prototype ingestion returned invalid JSON, retrying: {e}")
        llm_retry = get_llm(max_tokens=8192)
        response = llm_retry.invoke(
            [
                SystemMessage(content=PROTOTYPE_DATA_MODEL_PROMPT),
                HumanMessage(content=f"Return ONLY valid JSON. Keep it concise.\n\n{user_prompt}"),
            ]
        )
        try:
            result = parse_llm_json(response.content)
        except (json.JSONDecodeError, ValueError) as e2:
            logger.error(f"Prototype ingestion failed after retry: {e2}")
            return {
                "error": f"Failed to parse prototype analysis: {str(e2)[:200]}",
                "current_step": "prototype_ingestion",
                "completed_steps": [],
            }

    # 5. Extract results
    project_name = result.get("project_name", "unnamed-prototype")
    if state.get("project_name"):
        project_name = state["project_name"]

    entities = result.get("entities", [])
    relationships = result.get("relationships", [])

    logger.info(
        f"Prototype data model extracted: {len(entities)} entities, {len(relationships)} relationships",
        extra={"step": "prototype_ingestion", "project_name": project_name},
    )

    return {
        "is_sufficient": result.get("is_sufficient", True),
        "clarification_questions": result.get("clarification_questions", []),
        "project_name": project_name,
        "entities": entities,
        "relationships": relationships,
        "current_step": "prototype_ingestion",
        "completed_steps": ["prototype_ingestion"],
    }


def _build_user_prompt(prompt: str, file_contents: dict[str, str]) -> str:
    """Build the user prompt with file context."""
    parts = [
        f"## User Description\n{prompt}\n",
        f"\n## Prototype Source Files ({len(file_contents)} files)\n",
        "Extract the DATA MODEL from these files:\n",
    ]

    for filepath, content in file_contents.items():
        ext = filepath.rsplit(".", 1)[-1] if "." in filepath else ""
        parts.append(f"\n### {filepath}\n```{ext}\n{content}\n```\n")

    return "\n".join(parts)

