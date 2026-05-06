"""Deployment Config Service — Step 7 of both pipelines.

Generates app.yaml, Dockerfile, and .env.sample for Databricks Apps deployment.
Uses Jinja2 templates — no LLM call needed (deterministic).
"""

from lakebase_accelerator.models.generated_files import GeneratedFiles
from lakebase_accelerator.utils.logger import logger


class DeploymentConfigService:
    """Generates deployment configuration files for Databricks Apps."""

    async def execute(self, project_name: str, schema_name: str, app_name: str) -> GeneratedFiles:
        """Generate deployment config files.

        Args:
            project_name: Human-readable project name.
            schema_name: Lakebase schema name.
            app_name: Databricks App name.

        Returns:
            GeneratedFiles with app.yaml, Dockerfile, .env.sample.
        """
        logger.info(
            "Starting deployment config generation",
            extra={"step": "deployment_config", "app_name": app_name},
        )

        files = {
            "app.yaml": self._generate_app_yaml(app_name, schema_name),
            "Dockerfile": self._generate_dockerfile(),
            ".env.sample": self._generate_env_sample(schema_name),
        }

        logger.info("Deployment config generation complete", extra={"step": "deployment_config"})
        return GeneratedFiles(files=files)

    def _generate_app_yaml(self, app_name: str, schema_name: str) -> str:
        """Generate app.yaml for Databricks Apps."""
        return f"""# Databricks App Configuration
# App: {app_name}

command:
  - "uvicorn"
  - "main:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

resources:
  - name: "lakebase"
    type: "sql_warehouse"

env:
  - name: "LAKEBASE_SCHEMA"
    value: "{schema_name}"
  - name: "APP_NAME"
    value: "{app_name}"
"""

    def _generate_dockerfile(self) -> str:
        """Generate multi-stage Dockerfile for React + FastAPI."""
        return """# ─── Stage 1: Build React Frontend ───────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --production=false
COPY frontend/ ./
RUN npm run build

# ─── Stage 2: Python Runtime ─────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./

# Copy built frontend from Stage 1
COPY --from=frontend-build /app/frontend/dist ./static/

# Expose port and start
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

    def _generate_env_sample(self, schema_name: str) -> str:
        """Generate .env.sample documenting required environment variables."""
        return f"""# ─── Lakebase Connection (injected by Databricks Apps) ────────────
LAKEBASE_HOST=your-lakebase-host
LAKEBASE_PORT=5432
LAKEBASE_DATABASE=lakebase
LAKEBASE_USER=sp_generated_app
LAKEBASE_PASSWORD=auto_injected
LAKEBASE_SCHEMA={schema_name}

# ─── Application ─────────────────────────────────────────────────
APP_NAME=generated-app
"""
