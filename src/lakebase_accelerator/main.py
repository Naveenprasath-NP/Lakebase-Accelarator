"""FastAPI application entry point for the Lakebase Accelerator backend.

Initializes the Databricks WorkspaceClient and psycopg2 connection pool
at startup, registers all routes, and configures CORS.
Serves a test HTML page at / for local development testing.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env FIRST — before any other imports that might use env vars
from dotenv import load_dotenv

load_dotenv(override=True)

# Ensure MLflow uses Databricks SDK for OAuth (must be set before mlflow import)
os.environ["MLFLOW_ENABLE_DB_SDK"] = "true"

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from lakebase_accelerator.agent.tools import set_tool_dependencies
from lakebase_accelerator.client.lakebase_connection import create_lakebase_pool
from lakebase_accelerator.core.schema_init import initialize_accelerator_schema
from lakebase_accelerator.routes import default, projects
from lakebase_accelerator.services.dependencies import set_connection_pool, set_workspace_client
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.logger import logger

# Path to the static test page
STATIC_DIR = Path(__file__).parent.parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks.

    On startup:
    - Initializes Databricks WorkspaceClient (for Model Serving + Apps API).
    - Creates psycopg2 connection pool (for Lakebase).

    On shutdown:
    - Closes the connection pool.
    """
    settings = get_settings()
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

    # Initialize Databricks WorkspaceClient
    workspace_client = _init_workspace_client(settings)
    set_workspace_client(workspace_client)

    # Initialize Lakebase connection pool (with OAuth token rotation)
    lakebase_pool = create_lakebase_pool(workspace_client, settings)
    if lakebase_pool:
        set_connection_pool(lakebase_pool)
        # Initialize accelerator_meta schema (creates tables if not exist)
        initialize_accelerator_schema(lakebase_pool)
        # Set tool dependencies for LangGraph agent
        set_tool_dependencies(lakebase_pool, settings)
    else:
        logger.warning("Lakebase not connected — running in degraded mode")

    # Set environment variables for ChatDatabricks (langchain-databricks uses MLflow under the hood)
    os.environ.setdefault("DATABRICKS_HOST", settings.databricks_host)
    os.environ.setdefault("DATABRICKS_CLIENT_ID", settings.databricks_client_id)
    os.environ.setdefault("DATABRICKS_CLIENT_SECRET", settings.databricks_client_secret)

    logger.info("All dependencies initialized")
    yield

    # Shutdown: close connection pool
    if lakebase_pool:
        lakebase_pool.closeall()

    logger.info("Shutting down")


def _init_workspace_client(settings):
    """Initialize Databricks WorkspaceClient.

    In Databricks Apps, authentication is automatic via the app's service principal.
    For local development, uses either:
    - DATABRICKS_HOST + DATABRICKS_TOKEN (PAT auth)
    - DATABRICKS_HOST + DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (SP OAuth)
    """
    try:
        from databricks.sdk import WorkspaceClient

        # Prefer SP OAuth if client_id is set
        if settings.databricks_client_id and settings.databricks_client_secret:
            client = WorkspaceClient(
                host=settings.databricks_host,
                client_id=settings.databricks_client_id,
                client_secret=settings.databricks_client_secret,
            )
            logger.info(f"WorkspaceClient initialized with SP OAuth (host: {settings.databricks_host})")
        else:
            client = WorkspaceClient(
                host=settings.databricks_host or None,
                token=settings.databricks_token or None,
            )
            logger.info(f"WorkspaceClient initialized (host: {settings.databricks_host or 'auto'})")
        return client
    except Exception as e:
        logger.warning(f"Failed to initialize WorkspaceClient: {e}. Running in degraded mode.")
        return None


# ─── FastAPI App ─────────────────────────────────────────────────────

app = FastAPI(
    title="Lakebase Accelerator API",
    description="Backend API for the Zeb Agentic Lakebase Accelerator",
    version=get_settings().app_version,
    lifespan=lifespan,
)

# CORS — allow frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict to frontend origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(default.router)
app.include_router(projects.router)


# ─── Global Exception Handlers ───────────────────────────────────────


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handle Pydantic/FastAPI validation errors with standard envelope."""
    # Extract the first error message
    errors = exc.errors()
    if errors:
        first = errors[0]
        field = ".".join(str(loc) for loc in first.get("loc", []) if loc != "body")
        msg = first.get("msg", "Validation error")
        message = f"{field}: {msg}" if field else msg
    else:
        message = "Validation error"

    return JSONResponse(
        status_code=422,
        content={"success": False, "statusCode": 422, "message": message, "data": None},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unhandled exceptions."""
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "statusCode": 500,
            "message": "An unexpected error occurred. Please try again later.",
            "data": None,
        },
    )


# Serve test HTML page at root (for local development testing only)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_test_page():
    """Serve the test HTML page for local development."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Lakebase Accelerator API", "docs": "/docs"}
