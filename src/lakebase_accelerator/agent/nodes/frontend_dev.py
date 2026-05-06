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
    """Generate frontend as static HTML (no React build needed).

    NOTE: React build is disabled because:
    1. Node.js runtime is not available in Databricks Apps deployment environment
    2. The generated app uses a flat structure without Dockerfile (no multi-stage build)
    3. Static HTML with Tailwind CDN + vanilla JS works perfectly for CRUD apps

    TODO: Re-enable React build when Dockerfile-based deployment is implemented.
    """
    logger.info("Node: frontend_dev — generating static HTML", extra={"step": "frontend_dev"})

    data_model = state["data_model"]
    entities_summary = _summarize_entities(data_model)

    # Generate a static HTML page (no build step needed, works directly in Databricks Apps)
    frontend_files = _generate_static_fallback(entities_summary, data_model)

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


def _generate_static_fallback(entities_summary: str, data_model: dict) -> dict[str, str]:
    """Generate a simple static HTML page as fallback (no React build needed)."""
    llm = get_llm(max_tokens=8192)

    tables = data_model.get("tables", [])
    table_name = tables[0]["name"] if tables else "items"

    response = llm.invoke(
        [
            SystemMessage(
                content="Generate a COMPLETE single-page HTML app with inline JavaScript. Uses Tailwind CSS via CDN. Calls /api/{entity} for CRUD. No React, no build step needed. Return ONLY HTML."
            ),
            HumanMessage(
                content=f"Create a CRUD UI for entity: {table_name}\nFields: {entities_summary}\nAPI: GET/POST/PUT/DELETE /api/{table_name}"
            ),
        ]
    )

    html_content = _strip_markdown(response.content)

    return {
        "static/index.html": html_content,
    }


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
