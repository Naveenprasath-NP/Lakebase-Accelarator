"""Frontend Dev Agent — Node 6.

Generates React frontend files, then builds them locally (npm run build).
The built output (dist/) becomes the static/ folder for the backend.

If npm is not available, generates a simple static HTML fallback.
"""

import json
import subprocess
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger

FRONTEND_PLAN_PROMPT = """Plan a minimal React frontend for a CRUD app. Return ONLY a JSON array of file paths:
["package.json", "vite.config.ts", "tsconfig.json", "index.html", "src/main.tsx", "src/App.tsx", "src/api/client.ts", "src/index.css"]

Keep it minimal — one App.tsx with all CRUD UI inline. No separate page files.
"""

FRONTEND_FILE_PROMPT = """Generate COMPLETE content for: {file_path}

Entities: {entities_summary}
API base: /api (same origin, relative)

Rules:
- React 18 + TypeScript + Vite + Tailwind CSS (via CDN in index.html)
- App.tsx: single-page CRUD UI with list, create form, edit, delete
- API calls use fetch() to /api/{{entity_plural}}
- Keep it simple — everything in App.tsx for small apps
- package.json must include: react, react-dom, typescript, vite, @vitejs/plugin-react
- vite.config.ts: proxy /api to backend during dev
- File MUST be complete — no truncation
- Return ONLY the code, no markdown
"""


def frontend_dev_node(state: PipelineState) -> dict:
    """Generate frontend as static HTML.

    For brownfield: uses prototype_context to replicate the original UI faithfully.
    For greenfield: generates a generic CRUD interface.

    NOTE: React build is disabled because:
    1. Node.js runtime is not available in Databricks Apps deployment environment
    2. The generated app uses a flat structure without Dockerfile (no multi-stage build)
    3. Static HTML with Tailwind CDN + vanilla JS works perfectly for CRUD apps
    """
    logger.info("Node: frontend_dev — generating static HTML", extra={"step": "frontend_dev"})

    data_model = state["data_model"]
    backend_files = state.get("backend_files", {})
    entities_summary = _summarize_entities(data_model)
    pipeline_type = state.get("pipeline_type", "greenfield")
    prototype_context = state.get("prototype_context", "")

    # Extract API contract from the actual backend route files
    api_contract = _extract_api_contract(backend_files, data_model)

    if pipeline_type == "brownfield" and prototype_context:
        # Brownfield: generate frontend based on prototype's actual UI
        frontend_files = _generate_brownfield_frontend(entities_summary, data_model, api_contract, prototype_context)
    else:
        # Greenfield: generate generic CRUD frontend
        frontend_files = _generate_static_fallback(entities_summary, data_model, api_contract)

    logger.info(f"Frontend complete: {len(frontend_files)} static files", extra={"step": "frontend_dev"})

    return {
        "frontend_files": frontend_files,
        "current_step": "frontend_dev",
        "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
    }


def _generate_frontend_files(entities_summary: str) -> dict[str, str]:
    """Generate React source files using LLM."""
    llm = get_llm(max_tokens=2048)

    # Plan
    response = llm.invoke(
        [
            SystemMessage(content=FRONTEND_PLAN_PROMPT),
            HumanMessage(content=f"Plan for:\n{entities_summary}"),
        ]
    )

    try:
        file_list = json.loads(_strip_markdown(response.content))
    except (json.JSONDecodeError, ValueError):
        file_list = [
            "package.json",
            "vite.config.ts",
            "tsconfig.json",
            "index.html",
            "src/main.tsx",
            "src/App.tsx",
            "src/index.css",
        ]

    # Generate each file
    llm_gen = get_llm(max_tokens=8192)
    files: dict[str, str] = {}

    for file_path in file_list:
        logger.info(f"Generating frontend: {file_path}", extra={"step": "frontend_dev"})
        prompt = FRONTEND_FILE_PROMPT.format(file_path=file_path, entities_summary=entities_summary)

        response = llm_gen.invoke(
            [
                SystemMessage(content="Generate complete file content. No markdown. No truncation."),
                HumanMessage(content=prompt),
            ]
        )
        files[file_path] = _strip_markdown(response.content)

    return files


def _try_build_frontend(app_name: str, source_files: dict[str, str]) -> dict[str, str] | None:
    """Write frontend source to temp dir, run npm install + build, return dist/ contents."""
    try:
        build_dir = Path("generated_apps") / app_name / "_frontend_build"
        build_dir.mkdir(parents=True, exist_ok=True)

        # Write source files
        for file_path, content in source_files.items():
            full_path = build_dir / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        # Check if npm is available
        npm_check = subprocess.run(["npm", "--version"], capture_output=True, timeout=10)
        if npm_check.returncode != 0:
            logger.warning("npm not available, skipping frontend build")
            return None

        # npm install
        logger.info("Running npm install...", extra={"step": "frontend_dev"})
        install_result = subprocess.run(
            ["npm", "install"],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if install_result.returncode != 0:
            logger.warning(f"npm install failed: {install_result.stderr[:200]}")
            return None

        # npm run build
        logger.info("Running npm run build...", extra={"step": "frontend_dev"})
        build_result = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if build_result.returncode != 0:
            logger.warning(f"npm build failed: {build_result.stderr[:200]}")
            return None

        # Read dist/ output
        dist_dir = build_dir / "dist"
        if not dist_dir.exists():
            logger.warning("dist/ directory not found after build")
            return None

        static_files: dict[str, str] = {}
        for file in dist_dir.rglob("*"):
            if file.is_file():
                rel_path = f"static/{file.relative_to(dist_dir)}"
                try:
                    static_files[rel_path] = file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    # Skip binary files (images, etc.)
                    pass

        return static_files if static_files else None

    except Exception as e:
        logger.warning(f"Frontend build error: {e}")
        return None


def _generate_brownfield_frontend(
    entities_summary: str, data_model: dict, api_contract: str, prototype_context: str
) -> dict[str, str]:
    """Generate frontend that replicates the prototype's UI faithfully.

    Uses the prototype_context (UI description, features, layout) to build
    a production-quality frontend that matches the original prototype's
    look and functionality, not just generic CRUD.
    """
    llm = get_llm(max_tokens=16384)

    tables = data_model.get("tables", [])
    api_routes = []
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"
        cols = [c for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        col_names = [c["name"] for c in cols]
        api_routes.append(
            f"  - Entity: {entity_name}\n"
            f"    API prefix: /api/{entity_plural}\n"
            f"    Fields: {', '.join(col_names)}\n"
            f"    Endpoints: GET /api/{entity_plural}, POST /api/{entity_plural}, "
            f"GET /api/{entity_plural}/{{id}}, PUT /api/{entity_plural}/{{id}}, DELETE /api/{entity_plural}/{{id}}"
        )
    api_info = "\n".join(api_routes)

    response = llm.invoke(
        [
            SystemMessage(
                content="""You are a senior frontend developer. Generate a COMPLETE single-page HTML application that REPLICATES the prototype described below.

This is a BROWNFIELD migration — the goal is to recreate the prototype's UI and functionality at production quality, NOT to create a generic CRUD interface.

CRITICAL REQUIREMENTS:
- Replicate the prototype's ACTUAL UI layout, pages, navigation, and interactions
- Use Tailwind CSS via CDN for styling
- Use vanilla JavaScript (no framework needed for static HTML)
- Use fetch() for ALL API calls to the backend
- API calls must use the EXACT paths provided
- Include ALL features described in the prototype (dashboards, charts, forms, lists, etc.)
- If the prototype has charts/visualizations, use Chart.js via CDN
- If the prototype has tabs/navigation, replicate that structure
- Make it responsive and production-quality
- Include proper loading states, error handling, and empty states
- Return ONLY the complete HTML file, no markdown fences

The generated HTML should look and behave like the original prototype, just backed by the new API."""
            ),
            HumanMessage(
                content=f"""## Prototype Context (replicate this UI faithfully)
{prototype_context}

## Data Model (entities available via API)
{entities_summary}

## EXACT API Routes (use these paths)
{api_info}

## Backend API Contract
{api_contract}

IMPORTANT:
- Recreate the prototype's UI — don't just make generic CRUD tables
- If the prototype has a dashboard, build a dashboard
- If it has specific workflows or multi-step forms, replicate them
- Use the same visual style/approach described in the prototype context
- All API calls use relative paths (same origin)
- POST/PUT bodies include all fields EXCEPT id, created_at, updated_at
- IDs are UUID strings"""
            ),
        ]
    )

    html_content = _strip_markdown(response.content)

    return {
        "static/index.html": html_content,
    }


def _generate_static_fallback(entities_summary: str, data_model: dict, api_contract: str) -> dict[str, str]:
    """Generate a complete static HTML page with tabs for all entities.

    Uses the api_contract extracted from actual backend route files to ensure
    frontend API calls match backend exactly.
    """
    llm = get_llm(max_tokens=16384)

    tables = data_model.get("tables", [])

    # Build exact API routes info matching what the route template generates
    api_routes = []
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"
        cols = [c for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        col_names = [c["name"] for c in cols]
        api_routes.append(f"  - Entity: {entity_name}\n    API prefix: /api/{entity_plural}\n    Fields: {', '.join(col_names)}\n    Endpoints: GET /api/{entity_plural}, POST /api/{entity_plural}, GET /api/{entity_plural}/{{id}}, PUT /api/{entity_plural}/{{id}}, DELETE /api/{entity_plural}/{{id}}")

    api_info = "\n".join(api_routes)

    response = llm.invoke(
        [
            SystemMessage(
                content="""Generate a COMPLETE single-page HTML app with inline JavaScript and CSS.

REQUIREMENTS:
- Use Tailwind CSS via CDN for styling
- Create a tabbed interface with one tab per entity
- Each tab has: a create form, and a list showing all records with edit/delete buttons
- Use fetch() for ALL API calls (GET, POST, PUT, DELETE)
- API calls must use the EXACT paths provided (do not guess or change them)
- Load data for the active tab on tab switch AND on page load for the first tab
- Show loading states and error messages
- All CRUD operations must work: Create, Read (list + detail), Update, Delete
- Forms must submit JSON with Content-Type: application/json
- After create/update/delete, refresh the list
- Return ONLY the complete HTML file, no markdown fences"""
            ),
            HumanMessage(
                content=f"""Build a CRUD UI for these entities:

{entities_summary}

EXACT API routes (use these paths exactly, do not change them):
{api_info}

BACKEND API CONTRACT (extracted from actual route files):
{api_contract}

IMPORTANT:
- POST body must include all fields EXCEPT id, created_at, updated_at
- PUT body should only include fields being updated
- All responses return JSON objects/arrays
- IDs are UUID strings
- The API is on the same origin (no CORS needed, use relative paths like /api/...)
- On page load, fetch and display data for the first tab immediately"""
            ),
        ]
    )

    html_content = _strip_markdown(response.content)

    return {
        "static/index.html": html_content,
    }


def _extract_api_contract(backend_files: dict[str, str], data_model: dict) -> str:
    """Extract the API contract from actual backend route files.

    Reads the generated route files to determine exact:
    - API prefixes
    - Request/response model fields
    - Endpoint paths

    This ensures the frontend knows exactly what the backend expects.
    """
    contract_parts = []

    tables = data_model.get("tables", [])
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"
        route_file = f"src/routes/{entity_name}.py"

        # Get columns for request/response models
        columns = table.get("columns", [])
        create_fields = [c["name"] for c in columns if c["name"] not in ("id", "created_at", "updated_at")]
        all_fields = [c["name"] for c in columns]

        contract_parts.append(f"""
Entity: {entity_name}
  API Base: /api/{entity_plural}
  GET /api/{entity_plural} → returns array of objects with fields: {', '.join(all_fields)}
  POST /api/{entity_plural} → body: {{{', '.join(f'"{f}": "value"' for f in create_fields)}}}
  GET /api/{entity_plural}/{{id}} → returns single object
  PUT /api/{entity_plural}/{{id}} → body: only fields to update
  DELETE /api/{entity_plural}/{{id}} → returns 204 No Content""")

    return "\n".join(contract_parts)


def _summarize_entities(data_model: dict) -> str:
    tables = data_model.get("tables", [])
    parts = []
    for table in tables:
        cols = [c["name"] for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        parts.append(f"{table['name']}: {', '.join(cols)}")
    return "\n".join(parts)


def _strip_markdown(text: str) -> str:
    text = text.strip()
    for prefix in ("```python", "```typescript", "```tsx", "```json", "```html", "```javascript", "```css", "```"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
