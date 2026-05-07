"""Frontend Dev Agent — Node 6.

Generates React frontend files via LLM, then builds them using a
Databricks Job on a cluster with Node.js (since the accelerator app
environment does not have Node.js installed).

The built output (dist/) becomes the static/ folder for the backend.

For brownfield: uses prototype_context to replicate the original UI faithfully.
For greenfield: generates a generic CRUD interface.
Falls back to static HTML if the React build job fails.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from lakebase_accelerator.agent.llm import get_llm
from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.utils.logger import logger

FRONTEND_PLAN_PROMPT = """Plan a minimal React frontend for a CRUD app. Return ONLY a JSON array of file paths:
["package.json", "vite.config.ts", "tsconfig.json", "tsconfig.node.json", "index.html", "src/main.tsx", "src/App.tsx", "src/api/client.ts", "src/index.css"]

Keep it minimal — one App.tsx with all CRUD UI inline. No separate page files.
IMPORTANT: Always include tsconfig.node.json (required by vite.config.ts).
"""

FRONTEND_FILE_PROMPT = """Generate COMPLETE content for: {file_path}

Entities: {entities_summary}
API base: /api (same origin, relative)

Rules:
- React 18 + TypeScript + Vite + Tailwind CSS (via CDN in index.html OR via PostCSS)
- App.tsx: single-page CRUD UI with list, create form, edit, delete
- API calls use fetch() to /api/{{entity_plural}}
- Keep it simple — everything in App.tsx for small apps
- package.json must include: react, react-dom, typescript, vite, @vitejs/plugin-react
- vite.config.ts: use default build output (dist/), no custom outDir
- vite.config.ts: do NOT reference tsconfig.node.json unless you also generate it
- tsconfig.json: set "references": [{{"path": "./tsconfig.node.json"}}] only if tsconfig.node.json exists
- tsconfig.node.json: must include {{"compilerOptions": {{"composite": true, "module": "ESNext", "moduleResolution": "bundler"}}, "include": ["vite.config.ts"]}}
- index.html: must be at project root (not in src/), must have <div id="root"></div> and <script type="module" src="/src/main.tsx"></script>
- File MUST be complete — no truncation
- Return ONLY the code, no markdown

IMPORTANT for package.json:
- Include a "build" script: "vite build"
- Include exact versions for all dependencies (no ^ or ~ prefixes)
- Do NOT generate package-lock.json — npm install will create it automatically
"""


def frontend_dev_node(state: PipelineState) -> dict:
    """Generate React frontend and build it via a Databricks Job.

    For brownfield: uses prototype_context to replicate the original UI.
    For greenfield: generates a generic CRUD interface.

    Flow:
    1. Generate React source files using LLM (brownfield-aware)
    2. Submit a Databricks Job to run npm ci + npm run build on a cluster
    3. Retrieve the built dist/ output
    4. Return as static/ files for the bundle

    Falls back to static HTML if the build job fails.
    """
    import asyncio

    logger.info("Node: frontend_dev — generating React frontend", extra={"step": "frontend_dev"})

    data_model = state["data_model"]
    backend_files = state.get("backend_files", {})
    app_name = state.get("app_name", "generated-app")
    entities_summary = _summarize_entities(data_model)
    pipeline_type = state.get("pipeline_type", "greenfield")
    prototype_context = state.get("prototype_context", "")

    # Step 1: Generate React source files via LLM
    logger.info("Generating React source files via LLM...", extra={"step": "frontend_dev"})
    try:
        source_files = _generate_frontend_files(entities_summary, data_model, backend_files)
    except Exception as e:
        logger.warning(f"Frontend file generation failed: {e}", extra={"step": "frontend_dev"})
        source_files = None

    if not source_files:
        logger.warning("LLM failed to generate frontend files, using static fallback")
        api_contract = _extract_api_contract(backend_files, data_model)
        if pipeline_type == "brownfield" and prototype_context:
            frontend_files = _generate_brownfield_frontend(entities_summary, data_model, api_contract, prototype_context)
        else:
            frontend_files = _generate_static_fallback(entities_summary, data_model, api_contract)
        return {
            "frontend_files": frontend_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }

    # Step 2: Build via Databricks Job
    logger.info(
        f"Building React frontend via Databricks Job ({len(source_files)} source files)...",
        extra={"step": "frontend_dev"},
    )

    try:
        # Get workspace client from the tools module (same pattern as deployment node)
        from lakebase_accelerator.agent.tools import _get_workspace_client
        from lakebase_accelerator.services.frontend_build_service import FrontendBuildService

        workspace_client = _get_workspace_client()
        build_service = FrontendBuildService(workspace_client)

        # Run the async build — handle both sync and async calling contexts
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an async context (LangGraph runs nodes in async)
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run, build_service.build_frontend(app_name, source_files)
                )
                static_files = future.result(timeout=360)
        else:
            static_files = asyncio.run(build_service.build_frontend(app_name, source_files))

        logger.info(
            f"React build complete: {len(static_files)} static files",
            extra={"step": "frontend_dev"},
        )

        return {
            "frontend_files": static_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }

    except Exception as e:
        logger.warning(
            f"React build failed ({e}), falling back to static HTML",
            extra={"step": "frontend_dev"},
        )

        # Fallback: generate static HTML (works without Node.js)
        api_contract = _extract_api_contract(backend_files, data_model)
        if pipeline_type == "brownfield" and prototype_context:
            frontend_files = _generate_brownfield_frontend(entities_summary, data_model, api_contract, prototype_context)
        else:
            frontend_files = _generate_static_fallback(entities_summary, data_model, api_contract)

        return {
            "frontend_files": frontend_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }


def _generate_frontend_files(entities_summary: str, data_model: dict, backend_files: dict) -> dict[str, str] | None:
    """Generate React source files using LLM.

    Returns dict of file_path -> content, or None if generation fails.
    """
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
            "tsconfig.node.json",
            "index.html",
            "src/main.tsx",
            "src/App.tsx",
            "src/index.css",
        ]

    # Extract API contract from backend files for accurate frontend generation
    api_contract = _extract_api_contract(backend_files, data_model)

    # Generate each file
    llm_gen = get_llm(max_tokens=8192)
    files: dict[str, str] = {}

    for file_path in file_list:
        logger.info(f"Generating frontend: {file_path}", extra={"step": "frontend_dev"})

        if file_path == "package.json":
            prompt = _build_package_json_prompt(entities_summary)
        else:
            prompt = FRONTEND_FILE_PROMPT.format(file_path=file_path, entities_summary=entities_summary)
            prompt += f"\n\nAPI Contract (use these exact paths):\n{api_contract}"

        response = llm_gen.invoke(
            [
                SystemMessage(content="Generate complete file content. No markdown. No truncation."),
                HumanMessage(content=prompt),
            ]
        )
        files[file_path] = _strip_markdown(response.content)

    # Validate we got the critical files
    if "package.json" not in files:
        logger.warning("Missing package.json in generated files")
        return None

    return files


def _build_package_json_prompt(entities_summary: str) -> str:
    """Build a specific prompt for package.json to ensure correct build setup."""
    return f"""Generate a complete package.json for a React + Vite + TypeScript CRUD app.

Entities: {entities_summary}

REQUIREMENTS:
- name: use a simple lowercase name
- "private": true
- "type": "module"
- scripts:
  - "dev": "vite"
  - "build": "vite build"
  - "preview": "vite preview"
- dependencies (use EXACT versions, no ^ or ~):
  - "react": "18.2.0"
  - "react-dom": "18.2.0"
- devDependencies (use EXACT versions, no ^ or ~):
  - "@types/react": "18.2.45"
  - "@types/react-dom": "18.2.18"
  - "@vitejs/plugin-react": "4.2.1"
  - "typescript": "5.3.3"
  - "vite": "5.0.10"

Return ONLY the JSON, no markdown fences.
"""


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

    This is the fallback when the Databricks Job build is unavailable.
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
            f"    Endpoints: GET, POST, GET/id, PUT/id, DELETE/id"
        )
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
    """Extract the API contract from actual backend route files."""
    contract_parts = []

    tables = data_model.get("tables", [])
    for table in tables:
        entity_name = table["name"]
        entity_plural = entity_name if entity_name.endswith("s") else f"{entity_name}s"

        columns = table.get("columns", [])
        create_fields = [c["name"] for c in columns if c["name"] not in ("id", "created_at", "updated_at")]
        all_fields = [c["name"] for c in columns]

        contract_parts.append(
            f"Entity: {entity_name}\n"
            f"  API Base: /api/{entity_plural}\n"
            f"  GET /api/{entity_plural} - returns array of objects with fields: {', '.join(all_fields)}\n"
            f"  POST /api/{entity_plural} - body: {{{', '.join(f'{f}: value' for f in create_fields)}}}\n"
            f"  GET /api/{entity_plural}/{{id}} - returns single object\n"
            f"  PUT /api/{entity_plural}/{{id}} - body: only fields to update\n"
            f"  DELETE /api/{entity_plural}/{{id}} - returns 204 No Content"
        )

    return "\n\n".join(contract_parts)


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
            text = text[len(prefix):]
            break
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
