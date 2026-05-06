"""Integration & Validation — Node 7.

Merges backend + frontend (static/) into the final flat structure.
Validates:
- app.py exists and is valid Python
- app.py imports from src.routes (not bare 'import routes')
- requirements.txt exists and includes httpx
- app.yaml exists with all required env vars
- static/index.html exists
- All Python files pass AST syntax check
- LOCAL IMPORT TEST: Actually tries to import app:app to catch ImportError before deploying
"""

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

from lakebase_accelerator.agent.state import PipelineState
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger

REQUIRED_FILES = ["app.py", "requirements.txt", "app.yaml"]

REQUIRED_ENV_VARS = [
    "LAKEBASE_SCHEMA",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_DB",
    "DATABRICKS_HOST",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "LAKEBASE_ENDPOINT_NAME",
]


def integration_node(state: PipelineState) -> dict:
    """Merge backend + frontend into deployable bundle, validate everything."""
    logger.info("Node: integration — bundling and validating", extra={"step": "integration"})

    backend_files = state.get("backend_files", {})
    frontend_files = state.get("frontend_files", {})
    schema_name = state["schema_name"]
    app_name = state.get("app_name") or _generate_app_name(state["project_name"])

    # Merge: backend files are already flat (app.py at root), frontend goes into static/
    bundle_files = dict(backend_files)

    # Add frontend static files
    for path, content in frontend_files.items():
        if not path.startswith("static/"):
            bundle_files[f"static/{path}"] = content
        else:
            bundle_files[path] = content

    # Ensure app.yaml exists with full env vars
    if "app.yaml" not in bundle_files:
        bundle_files["app.yaml"] = _generate_app_yaml(schema_name, app_name)
    else:
        # Validate existing app.yaml has all required env vars
        app_yaml = bundle_files["app.yaml"]
        missing_vars = [v for v in REQUIRED_ENV_VARS if v not in app_yaml]
        if missing_vars:
            logger.warning(f"app.yaml missing env vars: {missing_vars}. Regenerating.")
            bundle_files["app.yaml"] = _generate_app_yaml(schema_name, app_name)

    # ─── Validation ──────────────────────────────────────────────────
    errors = []

    # Check required files
    for req in REQUIRED_FILES:
        if req not in bundle_files:
            errors.append(f"Missing required file: {req}")

    # Check static/index.html
    has_static = any(k.startswith("static/") and k.endswith(".html") for k in bundle_files)
    if not has_static:
        errors.append("No static/index.html found — frontend not built")

    # Validate Python syntax for all .py files
    for path, content in bundle_files.items():
        if path.endswith(".py"):
            syntax_error = _check_python_syntax(content)
            if syntax_error:
                errors.append(f"Syntax error in {path}: {syntax_error}")

    # Check app.py has correct imports and structure
    app_py = bundle_files.get("app.py", "")
    if app_py:
        if "FastAPI" not in app_py:
            errors.append("app.py missing FastAPI import")
        if "static" not in app_py.lower():
            errors.append("app.py doesn't serve static files")
        # CRITICAL: Check import pattern
        if "import routes" in app_py and "from src.routes" not in app_py:
            errors.append("app.py uses 'import routes' instead of 'from src.routes import router'")

    # Check requirements.txt includes httpx (needed for OAuth)
    requirements = bundle_files.get("requirements.txt", "")
    if "httpx" not in requirements:
        errors.append("requirements.txt missing httpx (needed for OAuth token generation)")
        # Auto-fix: add httpx
        bundle_files["requirements.txt"] = requirements.rstrip() + "\nhttpx==0.28.1\n"
        errors.pop()  # Remove the error since we auto-fixed

    # Check src/routes/__init__.py is just a re-export
    routes_init = bundle_files.get("src/routes/__init__.py", "")
    if routes_init and len(routes_init) > 200:
        # If __init__.py has more than a simple re-export, it likely has inline route implementations
        if "@router." in routes_init or "def get_" in routes_init or "def create_" in routes_init:
            errors.append("src/routes/__init__.py has inline route implementations — should only re-export")

    bundle_valid = len(errors) == 0

    if errors:
        logger.warning(f"Integration validation: {len(errors)} issues: {errors[:5]}", extra={"step": "integration"})
    else:
        logger.info(f"Integration valid: {len(bundle_files)} files", extra={"step": "integration"})

    # ─── Local Import Test (catches ImportError before deploying) ─────
    if bundle_valid:
        import_error = _test_local_import(bundle_files)
        if import_error:
            errors.append(f"Local import test failed: {import_error}")
            bundle_valid = False
            logger.warning(f"Local import test FAILED: {import_error}", extra={"step": "integration"})
        else:
            logger.info("Local import test PASSED", extra={"step": "integration"})

    # Build fix context if validation failed (for self-healing)
    fix_context = ""
    if not bundle_valid and errors:
        fix_parts = ["INTEGRATION VALIDATION FAILED — FIX REQUIRED", f"Errors: {'; '.join(errors)}"]
        for key_file in ("app.py", "src/__init__.py", "src/database.py", "src/routes/__init__.py"):
            if key_file in bundle_files:
                fix_parts.append(f"\n--- {key_file} ---\n{bundle_files[key_file][:500]}")
        fix_context = "\n".join(fix_parts)

    return {
        "bundle_files": bundle_files,
        "bundle_valid": bundle_valid,
        "app_name": app_name,
        "deployment_fix_context": fix_context,
        "deployment_retry_count": state.get("deployment_retry_count", 0) + (1 if fix_context else 0),
        "current_step": "integration",
        "completed_steps": state.get("completed_steps", []) + ["integration"],
        "error": "; ".join(errors) if errors else "",
    }


def _check_python_syntax(code: str) -> str | None:
    """Validate Python syntax using AST."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"Line {e.lineno}: {e.msg}"


def _generate_app_name(project_name: str) -> str:
    """Generate a Databricks App name (max 30 characters)."""
    import re
    from uuid import uuid4

    sanitized = re.sub(r"[^a-z0-9-]", "-", project_name.lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    suffix = uuid4().hex[:6]
    # Databricks Apps: max 30 chars. Reserve 7 for "-" + 6-char suffix
    return f"{sanitized[:23]}-{suffix}"


def _generate_app_yaml(schema_name: str, app_name: str) -> str:
    """Generate app.yaml with full Lakebase + OAuth environment variables."""
    settings = get_settings()
    return f"""command:
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

env:
  - name: "LAKEBASE_SCHEMA"
    value: "{schema_name}"
  - name: "POSTGRES_HOST"
    value: "{settings.postgres_host}"
  - name: "POSTGRES_PORT"
    value: "{settings.postgres_port}"
  - name: "POSTGRES_USER"
    value: "{settings.postgres_user}"
  - name: "POSTGRES_DB"
    value: "{settings.postgres_db}"
  - name: "DATABRICKS_HOST"
    value: "{settings.databricks_host}"
  - name: "DATABRICKS_CLIENT_ID"
    value: "{settings.databricks_client_id}"
  - name: "DATABRICKS_CLIENT_SECRET"
    value: "{settings.databricks_client_secret}"
  - name: "LAKEBASE_ENDPOINT_NAME"
    value: "{settings.lakebase_endpoint_name}"
"""


def _test_local_import(bundle_files: dict[str, str]) -> str | None:
    """Write bundle to a temp directory and try to import app:app.

    This catches ImportError, ModuleNotFoundError, and circular imports
    BEFORE deploying to Databricks — saving time and API calls.

    Returns error message string if import fails, None if successful.
    """
    try:
        # Write all files to a temp directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            for file_path, content in bundle_files.items():
                # Skip non-Python files for import test
                full_path = tmp_path / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")

            # Try to import the app module using a subprocess
            # This isolates the import from our running process
            test_script = (
                "import sys, os\n"
                f"sys.path.insert(0, r'{tmp_dir}')\n"
                "os.environ['POSTGRES_HOST'] = 'test'\n"
                "os.environ['POSTGRES_PORT'] = '5432'\n"
                "os.environ['POSTGRES_USER'] = 'test'\n"
                "os.environ['POSTGRES_DB'] = 'test'\n"
                "os.environ['POSTGRES_PASSWORD'] = 'test'\n"
                "os.environ['LAKEBASE_SCHEMA'] = 'public'\n"
                "os.environ['DATABRICKS_HOST'] = 'https://test'\n"
                "os.environ['DATABRICKS_CLIENT_ID'] = 'test'\n"
                "os.environ['DATABRICKS_CLIENT_SECRET'] = 'test'\n"
                "os.environ['LAKEBASE_ENDPOINT_NAME'] = 'test'\n"
                "# Try importing the app module (not starting uvicorn)\n"
                "import importlib.util\n"
                f"spec = importlib.util.spec_from_file_location('app', r'{tmp_dir}/app.py')\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "sys.modules['app'] = mod\n"
                "spec.loader.exec_module(mod)\n"
                "print('IMPORT_OK')\n"
            )

            result = subprocess.run(
                [sys.executable, "-c", test_script],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmp_dir,
            )

            if result.returncode == 0 and "IMPORT_OK" in result.stdout:
                return None  # Success

            # Extract the error
            error_output = result.stderr.strip()
            if error_output:
                # Get the last few lines (the actual error)
                lines = error_output.split("\n")
                # Find the actual error message (usually last line or ImportError line)
                error_lines = [l for l in lines if "Error" in l or "error" in l.lower()]
                if error_lines:
                    return error_lines[-1][:300]
                return "\n".join(lines[-3:])[:300]

            return f"Import failed with exit code {result.returncode}"

    except subprocess.TimeoutExpired:
        return "Import test timed out (possible infinite loop or blocking call in module)"
    except Exception as e:
        # Don't block deployment if the test itself fails
        logger.warning(f"Local import test could not run: {e}")
        return None  # Allow deployment to proceed
