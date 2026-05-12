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
from lakebase_accelerator.utils.prompt_loader import get_system_prompt


def _get_frontend_plan_prompt() -> str:
    """Load the frontend plan prompt from DB/YAML."""
    try:
        return get_system_prompt("frontend_plan")
    except KeyError:
        return FRONTEND_PLAN_PROMPT_FALLBACK


def _get_frontend_file_prompt() -> str:
    """Load the frontend file generation prompt from DB/YAML."""
    try:
        return get_system_prompt("frontend_file_generation")
    except KeyError:
        return FRONTEND_FILE_PROMPT_FALLBACK


def _get_frontend_static_prompt() -> str:
    """Load the frontend static fallback prompt from DB/YAML."""
    try:
        return get_system_prompt("frontend_static_fallback")
    except KeyError:
        return FRONTEND_STATIC_FALLBACK_PROMPT


def _get_frontend_brownfield_prompt() -> str:
    """Load the brownfield frontend prompt from DB/YAML."""
    try:
        return get_system_prompt("frontend_brownfield")
    except KeyError:
        return FRONTEND_BROWNFIELD_PROMPT_FALLBACK

FRONTEND_PLAN_PROMPT_FALLBACK = """Plan a minimal React frontend for a CRUD app with DARK THEME. Return ONLY a JSON array of file paths:
["package.json", "vite.config.ts", "tsconfig.json", "tsconfig.node.json", "index.html", "src/main.tsx", "src/App.tsx", "src/api/client.ts", "src/index.css"]

Keep it minimal — one App.tsx with all CRUD UI inline. No separate page files.
IMPORTANT: Always include tsconfig.node.json (required by vite.config.ts).
"""

FRONTEND_FILE_PROMPT_FALLBACK = """Generate COMPLETE content for: {file_path}

Entities: {entities_summary}
API base: /api (same origin, relative)

DESIGN SYSTEM (MUST follow — DARK THEME):
- Background: #0f172a (dark navy)
- Surface/cards: #1e293b (slate-800)
- Surface hover: #334155 (slate-700)
- Primary button: #3b82f6 (blue-500), hover: #2563eb
- Danger button: #ef4444, Success: #22c55e
- Text primary: #f1f5f9 (slate-100)
- Text secondary: #94a3b8 (slate-400)
- Text muted: #64748b (slate-500)
- Borders: #334155 (slate-700)
- Input background: #0f172a with border #334155
- Focus ring: box-shadow: 0 0 0 3px rgba(59,130,246,0.15)
- Layout: sidebar navigation (240px, bg #1e293b) + main content area
- Tables: full-width, uppercase headers, hover row highlight with #334155
- Buttons: rounded-lg, font-weight 600, hover translateY(-1px) + shadow
- Font: system-ui, -apple-system, sans-serif

Rules:
- React 18 + TypeScript + Vite
- DO NOT use Tailwind — use plain CSS with CSS custom properties (variables)
- src/index.css: define :root with all design tokens above, global styles
- App.tsx: sidebar + main content layout, CRUD UI with dark theme
- API calls use fetch() to /api/{entity_plural}
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

    # ─── Template-based React generation ──────────────────────────────
    # 1. Render fixed template files (package.json, vite.config, tsconfig, etc.)
    # 2. Single LLM call to generate ONLY App.tsx (with full context)
    # 3. Submit to Databricks Job for build
    # 4. Fallback to static HTML if build fails
    logger.info("Generating React frontend (template + LLM for App.tsx)...", extra={"step": "frontend_dev"})

    project_name = state.get("project_name", "generated-app")
    project_title = project_name.replace("-", " ").title()
    api_contract = _extract_api_contract(backend_files, data_model)

    # Step 1: Render template files
    source_files = _render_frontend_templates(project_name, project_title)

    # Step 2: Generate App.tsx via LLM (single call with full context)
    try:
        app_tsx = _generate_app_tsx(entities_summary, api_contract, data_model, pipeline_type, prototype_context)
        source_files["src/App.tsx"] = app_tsx
    except Exception as e:
        logger.warning(f"App.tsx generation failed: {e}, using static fallback", extra={"step": "frontend_dev"})
        frontend_files = _generate_static_fallback(entities_summary, data_model, api_contract)
        return {
            "frontend_files": frontend_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }

    # Step 3: Build via Databricks Job
    logger.info(f"Building React frontend ({len(source_files)} files)...", extra={"step": "frontend_dev"})

    try:
        from lakebase_accelerator.agent.tools import _get_workspace_client
        from lakebase_accelerator.services.frontend_build_service import FrontendBuildService
        import asyncio
        import concurrent.futures

        workspace_client = _get_workspace_client()
        build_service = FrontendBuildService(workspace_client)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, build_service.build_frontend(app_name, source_files))
                static_files = future.result(timeout=360)
        else:
            static_files = asyncio.run(build_service.build_frontend(app_name, source_files))

        logger.info(f"React build complete: {len(static_files)} static files", extra={"step": "frontend_dev"})

        return {
            "frontend_files": static_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }

    except Exception as e:
        logger.warning(f"React build failed ({e}), falling back to static HTML", extra={"step": "frontend_dev"})
        frontend_files = _generate_static_fallback(entities_summary, data_model, api_contract)
        return {
            "frontend_files": frontend_files,
            "current_step": "frontend_dev",
            "completed_steps": state.get("completed_steps", []) + ["frontend_dev"],
        }
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


def _render_frontend_templates(project_name: str, project_title: str) -> dict[str, str]:
    """Render fixed frontend template files using Jinja2.

    These files never change — they're the scaffolding that App.tsx plugs into.
    """
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    template_dir = Path(__file__).parent.parent.parent / "resources" / "templates" / "frontend"
    env = Environment(loader=FileSystemLoader(str(template_dir)))

    context = {"project_name": project_name, "project_title": project_title}

    files: dict[str, str] = {}
    template_map = {
        "package.json": "package.json.j2",
        "vite.config.ts": "vite.config.ts.j2",
        "tsconfig.json": "tsconfig.json.j2",
        "tsconfig.node.json": "tsconfig.node.json.j2",
        "index.html": "index.html.j2",
        "src/main.tsx": "main.tsx.j2",
        "src/index.css": "index.css.j2",
    }

    for output_path, template_name in template_map.items():
        template = env.get_template(template_name)
        files[output_path] = template.render(**context)

    return files


def _generate_app_tsx(
    entities_summary: str,
    api_contract: str,
    data_model: dict,
    pipeline_type: str,
    prototype_context: str,
) -> str:
    """Generate App.tsx via a single LLM call with full context.

    The LLM only generates the React component code — all config files
    and CSS are handled by templates.

    For brownfield: the prototype context drives the UI layout and design.
    For greenfield: generates a generic sidebar CRUD interface.
    """
    # Scale max_tokens based on complexity (number of entities)
    tables = data_model.get("tables", [])
    num_entities = len(tables)
    # Complex apps (5+ entities) need more tokens to avoid truncation
    if num_entities >= 5:
        max_tokens = 64000
    else:
        max_tokens = 64000

    llm = get_llm(max_tokens=max_tokens)

    # Build entity details for the prompt
    tables = data_model.get("tables", [])
    entity_details = []
    for table in tables:
        cols = [c for c in table.get("columns", []) if c["name"] not in ("id", "created_at", "updated_at")]
        entity_details.append({
            "name": table["name"],
            "plural": table["name"] if table["name"].endswith("s") else f"{table['name']}s",
            "columns": [{"name": c["name"], "type": c["data_type"], "nullable": c.get("nullable", True)} for c in cols],
        })

    if pipeline_type == "brownfield" and prototype_context:
        # Brownfield: prototype context is the PRIMARY design instruction
        # For complex apps, instruct LLM to focus on the visible page only
        complexity_note = ""
        if num_entities >= 5:
            complexity_note = (
                "\n\n## IMPORTANT: Keep Code Concise\n"
                "This app has many entities. To avoid code truncation:\n"
                "- Focus on the MAIN page shown in the prototype (e.g., the Orders list)\n"
                "- For sidebar navigation items, show placeholder pages with just a title\n"
                "- Only implement full CRUD for the PRIMARY entity shown in the prototype\n"
                "- Other entities get simple list views with basic fetch\n"
                "- Keep the code under 1500 lines total\n"
            )

        prompt = f"""Generate a COMPLETE React App.tsx component that REPLICATES the user's prototype UI.

## CRITICAL: Replicate This UI Design
{prototype_context[:4000]}{complexity_note}

## IMPORTANT INSTRUCTIONS
- You MUST replicate the prototype's EXACT layout, theme, colors, and component structure
- Do NOT use a generic sidebar + CRUD table layout unless the prototype specifically shows one
- Match the prototype's visual style: colors, spacing, typography, component arrangement
- If the prototype shows a light theme, use light colors (NOT dark navy backgrounds)
- If the prototype shows inline lists with checkboxes, build that (NOT data tables)
- If the prototype shows filter tabs, build filter tabs (NOT sidebar navigation)
- The UI should look like the prototype, just backed by the real API

## Entities (data available via API)
{json.dumps(entity_details, indent=2)}

## API Contract
{api_contract}

## Technical Requirements
- Export a default App component
- Use fetch() for API calls to /api/{{entity_plural}} (same origin, relative paths)
- Use React useState and useEffect hooks for state management
- Include loading states — use a div with className "spinner" (shows "Loading" text spinning in a circle)
- Include error handling (show error message if API fails)
- After create/update/delete, refresh the data
- Use inline styles or CSS-in-JS to match the prototype's exact theme and colors
- You MAY also use className references from index.css where they fit the prototype's design:
  - Buttons: "btn btn-primary", "btn btn-danger", "btn btn-secondary"
  - States: "spinner", "empty-state"
  - Modal: "modal-overlay", "modal", "modal-title", "modal-actions"
- If the prototype's design conflicts with index.css classes, use inline styles to match the prototype
- Do NOT import any CSS file (index.css is already imported in main.tsx)
- Do NOT use Tailwind classes
- Return ONLY the TypeScript/React code, no markdown fences
- The file MUST be complete and syntactically valid — no truncation"""

        system_msg = (
            "You are a senior frontend developer. Generate a complete, valid React TypeScript component "
            "that FAITHFULLY replicates the user's prototype UI design. The prototype's visual design "
            "takes priority over any generic patterns. Match the layout, theme, colors, and interactions "
            "shown in the prototype. Return ONLY code, no markdown."
        )
    else:
        # Greenfield: generic CRUD interface with sidebar layout
        prompt = f"""Generate a COMPLETE React App.tsx component for a CRUD application.

## Entities
{json.dumps(entity_details, indent=2)}

## API Contract
{api_contract}

## Requirements
- Export a default App component
- Use a sidebar layout: left sidebar with nav links for each entity, main content area on the right
- Implement full CRUD for each entity: List (table), Create (form), Edit (form), Delete (button)
- Use fetch() for API calls to /api/{{entity_plural}} (same origin, relative paths)
- Use React useState and useEffect hooks for state management
- Show the active entity's data in the main content area
- Tables should show all columns (except id, created_at, updated_at)
- Forms should have inputs for all editable columns
- Include loading states — use a div with className "spinner" (shows "Loading" text spinning in a circle)
- Include error handling (show error message if API fails)
- After create/update/delete, refresh the list
- Use className references to the CSS classes defined in index.css:
  - Layout: "app-layout", "sidebar", "sidebar-title", "nav-item", "nav-item active", "main-content"
  - Page: "page-header", "page-title"
  - Buttons: "btn btn-primary", "btn btn-danger", "btn btn-secondary", "btn-sm"
  - Cards: "card", "card-body"
  - Tables: "data-table", "actions"
  - Forms: "form-group", "form-label", "form-input", "form-select", "form-textarea"
  - States: "spinner", "empty-state"
  - Modal: "modal-overlay", "modal", "modal-title", "modal-actions"
- Do NOT import any CSS file (index.css is already imported in main.tsx)
- Do NOT use Tailwind classes
- Return ONLY the TypeScript/React code, no markdown fences
- The file MUST be complete and syntactically valid — no truncation"""

        system_msg = "Generate a complete, valid React TypeScript component. Return ONLY code, no markdown. The component must compile without errors."

    response = llm.invoke(
        [
            SystemMessage(content=system_msg),
            HumanMessage(content=prompt),
        ]
    )

    return _strip_markdown(response.content)


def _generate_frontend_files(entities_summary: str, data_model: dict, backend_files: dict) -> dict[str, str] | None:
    """Generate React source files using LLM.

    Returns dict of file_path -> content, or None if generation fails.
    """
    llm = get_llm(max_tokens=2048)

    # Plan
    response = llm.invoke(
        [
            SystemMessage(content=_get_frontend_plan_prompt()),
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
    llm_gen = get_llm(max_tokens=16384)
    files: dict[str, str] = {}

    for file_path in file_list:
        logger.info(f"Generating frontend: {file_path}", extra={"step": "frontend_dev"})

        if file_path == "package.json":
            prompt = _build_package_json_prompt(entities_summary)
        else:
            template = _get_frontend_file_prompt()
            # Safe replacement — only replace known placeholders, leave others intact
            prompt = template.replace("{file_path}", file_path).replace("{entities_summary}", entities_summary)
            prompt += f"\n\nAPI Contract (use these exact paths):\n{api_contract}"

        response = llm_gen.invoke(
            [
                SystemMessage(content=f"Generate ONLY the content for the file '{file_path}'. "
                              f"This is a {file_path.split('.')[-1]} file. "
                              f"Do NOT include React components in config files. "
                              f"Do NOT include JSON in .ts/.tsx files. "
                              f"Return ONLY valid content for this specific file type. No markdown."),
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
- REPLICATE the prototype's EXACT visual design: theme, colors, layout, and component structure
- If the prototype uses a LIGHT theme, use light colors (cream/white backgrounds, dark text)
- If the prototype uses a DARK theme, use dark colors (navy backgrounds, light text)
- Do NOT force a dark theme if the prototype shows a light theme
- Match the prototype's layout: if it shows a single-page list, build that (NOT a sidebar + tables)
- Match the prototype's interactions: checkboxes, filter tabs, inline editing, etc.
- Use CSS custom properties for theming
- Use vanilla JavaScript (no framework needed for static HTML)
- Use fetch() for ALL API calls to the backend
- API calls must use the EXACT paths provided
- Include ALL features described in the prototype (filters, badges, clear completed, etc.)
- Loading state: show a spinning circle with "Loading" text inside it (text rotates with the circle)
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
- Use a DARK THEME design system with these colors:
  - Background: #0f172a (dark navy)
  - Surface/cards: #1e293b (slate-800)
  - Surface hover: #334155 (slate-700)
  - Primary button: #3b82f6 (blue-500), hover: #2563eb
  - Danger button: #ef4444
  - Success: #22c55e
  - Text primary: #f1f5f9 (slate-100)
  - Text secondary: #94a3b8 (slate-400)
  - Text muted: #64748b (slate-500)
  - Borders: #334155 (slate-700)
  - Input background: #0f172a with border #334155
  - Focus ring: box-shadow: 0 0 0 3px rgba(59,130,246,0.15)
- Layout: sidebar navigation (260px, bg #1e293b) + main content area (padding 32px)
- Sidebar: app name at top, nav links for each entity (active = blue tint background)
- Tables: full-width inside cards, uppercase header labels (text-xs, letter-spacing), hover row highlight
- Buttons: rounded-lg (8px), font-weight 600, hover lifts with shadow
- Cards: bg #1e293b, border #334155, rounded-xl (12px), shadow
- Forms: dark inputs (bg #0f172a, border #334155), blue focus ring, labels above
- Font: Inter (system-ui fallback), 14px body, 24px page titles
- All transitions: 150ms ease
- Use CSS custom properties for theming (define :root variables)
- Create a tabbed interface via sidebar with one section per entity
- Each section has: a create form, and a data table showing all records with edit/delete buttons
- Use fetch() for ALL API calls (GET, POST, PUT, DELETE)
- API calls must use the EXACT paths provided (do not guess or change them)
- Load data for the active section on nav click AND on page load for the first entity
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
