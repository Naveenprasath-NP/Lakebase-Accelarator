"""Greenfield Pipeline Service — Orchestrates the 12-step greenfield pipeline.

Executes steps sequentially (with parallel frontend+backend generation),
yields SSE events for real-time progress, and handles step-level errors.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

from lakebase_accelerator.models.enums import PipelineStep, StepStatus
from lakebase_accelerator.models.pipeline import PipelineEvent
from lakebase_accelerator.services.app_deployment_service import AppDeploymentService
from lakebase_accelerator.services.audit_service import AuditService
from lakebase_accelerator.services.backend_generation_service import BackendGenerationService
from lakebase_accelerator.services.data_model_inference_service import DataModelInferenceService
from lakebase_accelerator.services.deployment_config_service import DeploymentConfigService
from lakebase_accelerator.services.frontend_generation_service import FrontendGenerationService
from lakebase_accelerator.services.permission_service import PermissionService
from lakebase_accelerator.services.requirement_intake_service import RequirementIntakeService
from lakebase_accelerator.services.schema_provisioning_service import SchemaProvisioningService
from lakebase_accelerator.services.seed_data_service import SeedDataService
from lakebase_accelerator.services.validation_service import ValidationService
from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import PIPELINE_TIMEOUT
from lakebase_accelerator.utils.logger import logger


class GreenFieldPipelineService:
    """Orchestrates the greenfield pipeline from prompt to deployed app URL."""

    def __init__(
        self,
        requirement_intake: RequirementIntakeService,
        data_model_inference: DataModelInferenceService,
        schema_provisioning: SchemaProvisioningService,
        seed_data: SeedDataService,
        frontend_generation: FrontendGenerationService,
        backend_generation: BackendGenerationService,
        deployment_config: DeploymentConfigService,
        validation: ValidationService,
        app_deployment: AppDeploymentService,
        permission: PermissionService,
        audit: AuditService,
    ) -> None:
        self._requirement_intake = requirement_intake
        self._data_model_inference = data_model_inference
        self._schema_provisioning = schema_provisioning
        self._seed_data = seed_data
        self._frontend_generation = frontend_generation
        self._backend_generation = backend_generation
        self._deployment_config = deployment_config
        self._validation = validation
        self._app_deployment = app_deployment
        self._permission = permission
        self._audit = audit

    async def execute(self, prompt: str, project_name: str | None = None) -> AsyncGenerator[PipelineEvent, None]:
        """Execute the full greenfield pipeline as an async generator yielding SSE events.

        Args:
            prompt: Natural-language business prompt.
            project_name: Optional project name (auto-inferred if None).

        Yields:
            PipelineEvent objects for each step transition and final result.
        """
        settings = get_settings()
        pipeline_start = time.time()
        completed_steps: list[str] = []
        project_id: str | None = None

        try:
            # ─── Step 1: Requirement Intake ──────────────────────────────
            yield self._step_started(PipelineStep.REQUIREMENT_INTAKE, "Analyzing business prompt...")

            analysis = await asyncio.wait_for(
                self._requirement_intake.execute(prompt),
                timeout=settings.pipeline_timeout_seconds,
            )

            # Use inferred project name if not provided
            if not project_name:
                project_name = analysis.project_name

            completed_steps.append("requirement_intake")
            yield self._step_completed(
                PipelineStep.REQUIREMENT_INTAKE,
                f"Identified {len(analysis.entities)} entities, {len(analysis.relationships)} relationships",
                {"entity_count": len(analysis.entities), "relationship_count": len(analysis.relationships)},
            )

            # ─── Step 2: Data Model Inference ────────────────────────────
            yield self._step_started(PipelineStep.DATA_MODEL_INFERENCE, "Generating PostgreSQL data model...")

            data_model = await self._data_model_inference.execute(analysis)

            completed_steps.append("data_model_inference")
            yield self._step_completed(
                PipelineStep.DATA_MODEL_INFERENCE,
                f"Generated {len(data_model.tables)} tables",
                {"table_count": len(data_model.tables), "tables": data_model.creation_order},
            )

            # ─── Step 3: Schema Provisioning ─────────────────────────────
            yield self._step_started(PipelineStep.SCHEMA_PROVISIONING, "Creating Lakebase schema and tables...")

            schema_name, table_names = await self._schema_provisioning.execute(data_model, project_name)

            completed_steps.append("schema_provisioning")
            yield self._step_completed(
                PipelineStep.SCHEMA_PROVISIONING,
                f"Created schema '{schema_name}' with {len(table_names)} tables",
                {"schema_name": schema_name, "tables": table_names},
            )

            # Create audit record now that we have schema_name
            project_id = await self._audit.create_project_record(
                project_name=project_name,
                schema_name=schema_name,
                mode="greenfield",
                prompt=prompt,
            )

            # ─── Step 4: Seed Data Generation ────────────────────────────
            yield self._step_started(PipelineStep.SEED_DATA_GENERATION, "Generating sample data...")

            row_counts = await self._seed_data.execute(schema_name, data_model)

            completed_steps.append("seed_data_generation")
            yield self._step_completed(
                PipelineStep.SEED_DATA_GENERATION,
                f"Inserted {sum(row_counts.values())} rows across {len(row_counts)} tables",
                {"row_counts": row_counts},
            )

            # ─── Steps 5-6: Frontend + Backend Generation (parallel) ─────
            yield self._step_started(PipelineStep.FRONTEND_GENERATION, "Generating React frontend...")
            yield self._step_started(PipelineStep.BACKEND_GENERATION, "Generating FastAPI backend...")

            # Generate app_name early — needed for the frontend build step
            app_name = self._generate_app_name(project_name)

            frontend_files, backend_files = await asyncio.gather(
                self._frontend_generation.execute(data_model, project_name),
                self._backend_generation.execute(data_model, schema_name, project_name),
            )

            completed_steps.append("frontend_generation")
            yield self._step_completed(
                PipelineStep.FRONTEND_GENERATION,
                f"Generated {len(frontend_files.files)} frontend files",
                {"file_count": len(frontend_files.files)},
            )

            completed_steps.append("backend_generation")
            yield self._step_completed(
                PipelineStep.BACKEND_GENERATION,
                f"Generated {len(backend_files.files)} backend files",
                {"file_count": len(backend_files.files)},
            )

            # ─── Step 5b: Build React Frontend via Databricks Job ────────
            yield self._step_started(PipelineStep.FRONTEND_GENERATION, "Building React frontend...")

            from lakebase_accelerator.services.frontend_build_service import FrontendBuildService

            build_service = FrontendBuildService(self._get_workspace_client())

            # Strip "frontend/" prefix for the build service (it expects flat paths like package.json, src/App.tsx)
            source_files_for_build = {
                k.removeprefix("frontend/"): v
                for k, v in frontend_files.files.items()
                if k.startswith("frontend/")
            }

            try:
                built_frontend = await build_service.build_frontend(app_name, source_files_for_build)
                # built_frontend has paths like "static/index.html", "static/assets/index-abc.js"
                # Replace the raw frontend source files with built output
                frontend_files = type(frontend_files)(files=built_frontend)
                logger.info(
                    f"React build complete: {len(built_frontend)} static files",
                    extra={"step": "frontend_build"},
                )
            except Exception as e:
                logger.warning(
                    f"React build failed ({e}), deploying frontend source files as-is",
                    extra={"step": "frontend_build"},
                )
                # Keep the original frontend_files — they'll be deployed as source
                # The Dockerfile in the bundle can still build them if Docker is available

            # ─── Step 7: Deployment Config ───────────────────────────────
            yield self._step_started(PipelineStep.DEPLOYMENT_CONFIG, "Generating deployment configuration...")

            config_files = await self._deployment_config.execute(project_name, schema_name, app_name)

            completed_steps.append("deployment_config")
            yield self._step_completed(
                PipelineStep.DEPLOYMENT_CONFIG,
                "Generated app.yaml, Dockerfile, .env.sample",
                {"files": list(config_files.files.keys())},
            )

            # ─── Merge all files ─────────────────────────────────────────
            all_files = {**frontend_files.files, **backend_files.files, **config_files.files}

            # ─── Step 8: Validation ──────────────────────────────────────
            yield self._step_started(PipelineStep.VALIDATION, "Validating generated application...")

            await self._validation.execute(schema_name, table_names, all_files)

            completed_steps.append("validation")
            yield self._step_completed(
                PipelineStep.VALIDATION,
                "All validation checks passed",
                {},
            )

            # ─── Step 9: Workspace Write ─────────────────────────────────
            yield self._step_started(PipelineStep.WORKSPACE_WRITE, "Writing files to Databricks Workspace...")

            workspace_path = await self._app_deployment.write_files(app_name, all_files)

            completed_steps.append("workspace_write")
            yield self._step_completed(
                PipelineStep.WORKSPACE_WRITE,
                f"Wrote {len(all_files)} files to {workspace_path}",
                {"workspace_path": workspace_path, "file_count": len(all_files)},
            )

            # ─── Step 10: App Deployment ─────────────────────────────────
            yield self._step_started(PipelineStep.APP_DEPLOYMENT, "Deploying Databricks App...")

            deployment_status = await self._app_deployment.deploy(app_name, workspace_path, schema_name)

            completed_steps.append("app_deployment")
            yield self._step_completed(
                PipelineStep.APP_DEPLOYMENT,
                f"App deployed: {deployment_status.app_url}",
                {"app_url": deployment_status.app_url},
            )

            # ─── Step 11: Permission Grant ───────────────────────────────
            yield self._step_started(PipelineStep.PERMISSION_GRANT, "Granting schema permissions...")

            sp_id = await self._permission.execute(app_name, schema_name)

            completed_steps.append("permission_grant")
            yield self._step_completed(
                PipelineStep.PERMISSION_GRANT,
                f"Granted access to SP: {sp_id}",
                {"service_principal_id": sp_id},
            )

            # ─── Step 12: Audit ──────────────────────────────────────────
            yield self._step_started(PipelineStep.AUDIT, "Recording deployment metadata...")

            pipeline_duration = time.time() - pipeline_start
            if project_id:
                await self._audit.update_project_completed(
                    project_id=project_id,
                    app_name=app_name,
                    app_url=deployment_status.app_url or "",
                    service_principal_id=sp_id,
                    tables_created=table_names,
                    pipeline_duration_seconds=pipeline_duration,
                    total_token_usage=0,  # TODO: Track token usage across steps
                )

            completed_steps.append("audit")
            yield self._step_completed(PipelineStep.AUDIT, "Audit record saved", {})

            # ─── Pipeline Complete ───────────────────────────────────────
            yield PipelineEvent(
                event_type="pipeline_complete",
                step=None,
                status=StepStatus.COMPLETED,
                message="App deployed successfully",
                data={
                    "app_url": deployment_status.app_url,
                    "app_name": app_name,
                    "schema_name": schema_name,
                    "catalog": settings.catalog_name,
                    "tables_created": table_names,
                    "completed_steps": completed_steps,
                    "pipeline_duration_seconds": round(pipeline_duration, 1),
                },
            )

        except TimeoutError:
            pipeline_duration = time.time() - pipeline_start
            if project_id:
                await self._audit.update_project_failed(project_id, "timeout", "Pipeline timeout exceeded")
            yield self._pipeline_failed(
                "Pipeline timeout exceeded",
                PIPELINE_TIMEOUT,
                completed_steps,
            )

        except PipelineStepError as e:
            pipeline_duration = time.time() - pipeline_start
            logger.error(f"Pipeline step failed: {e.step_name} — {e.message}", extra={"step": e.step_name})
            if project_id:
                await self._audit.update_project_failed(project_id, e.step_name, e.message)

            yield PipelineEvent(
                event_type="step_failed",
                step=PipelineStep(e.step_name) if e.step_name in PipelineStep._value2member_map_ else None,
                status=StepStatus.FAILED,
                message=e.message,
                data={"error_code": e.error_code, **e.details},
            )
            yield self._pipeline_failed(e.message, e.error_code, completed_steps, e.step_name)

        except Exception as e:
            pipeline_duration = time.time() - pipeline_start
            logger.exception(f"Unexpected pipeline error: {e}")
            if project_id:
                await self._audit.update_project_failed(project_id, "unknown", str(e))
            yield self._pipeline_failed(str(e), "UNEXPECTED_ERROR", completed_steps)

    # ─── Helper Methods ──────────────────────────────────────────────

    def _step_started(self, step: PipelineStep, message: str) -> PipelineEvent:
        return PipelineEvent(event_type="step_started", step=step, status=StepStatus.RUNNING, message=message)

    def _step_completed(self, step: PipelineStep, message: str, data: dict) -> PipelineEvent:
        return PipelineEvent(
            event_type="step_completed", step=step, status=StepStatus.COMPLETED, message=message, data=data
        )

    def _pipeline_failed(
        self, message: str, error_code: str, completed_steps: list[str], failed_step: str | None = None
    ) -> PipelineEvent:
        return PipelineEvent(
            event_type="pipeline_complete",
            step=None,
            status=StepStatus.FAILED,
            message=f"Pipeline failed: {message}",
            data={
                "error_code": error_code,
                "failed_step": failed_step,
                "completed_steps": completed_steps,
            },
        )

    def _generate_app_name(self, project_name: str) -> str:
        """Generate a Databricks App name from the project name."""
        # Databricks App names: lowercase, alphanumeric + hyphens, max 63 chars
        import re
        from uuid import uuid4

        sanitized = re.sub(r"[^a-z0-9-]", "-", project_name.lower())
        sanitized = re.sub(r"-+", "-", sanitized).strip("-")
        suffix = uuid4().hex[:6]
        max_len = 63 - len(suffix) - 1
        return f"{sanitized[:max_len]}-{suffix}"

    def _get_workspace_client(self):
        """Get a Databricks WorkspaceClient instance."""
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient()
