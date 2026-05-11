"""Project routes — unified pipeline execution, list, and detail endpoints."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from lakebase_accelerator.agent.graph import get_pipeline_graph
from lakebase_accelerator.agent.nodes.checkpoint import (
    CONFIRMATION_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    get_pending_checkpoint,
    is_awaiting_confirmation,
    submit_confirmation,
)
from lakebase_accelerator.models.enums import PipelineStatus, ProjectMode
from lakebase_accelerator.models.requests import ConfirmationRequest
from lakebase_accelerator.models.responses import (
    ApiResponse,
    ProjectDetailData,
    ProjectListData,
    ProjectSummary,
    error_response,
    success_response,
)
from lakebase_accelerator.services.dependencies import get_audit_service, get_volume_upload_service
from lakebase_accelerator.services.volume_upload_service import FileValidationError
from lakebase_accelerator.settings import PROMPT_MAX_LENGTH, PROMPT_MIN_LENGTH
from lakebase_accelerator.utils.logger import logger

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


# ═══════════════════════════════════════════════════════════════════════
# UNIFIED PIPELINE ENDPOINT (SSE)
# ═══════════════════════════════════════════════════════════════════════


@router.post("/execute", response_model=None)
async def execute_pipeline(
    type: str = Form(...),
    prompt: str = Form(...),
    project_name: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
) -> StreamingResponse | JSONResponse:
    """Execute the accelerator pipeline via multi-agent LangGraph orchestrator.

    Accepts multipart/form-data for both greenfield and brownfield:
    - Greenfield: type=greenfield, prompt=<text> (no files needed)
    - Brownfield: type=brownfield, prompt=<text>, files=<binary uploads>

    Returns an SSE stream with real-time progress as each node executes.
    """
    # ─── Validate type ───────────────────────────────────────────────
    if type not in ("greenfield", "brownfield"):
        resp = error_response(message="type must be 'greenfield' or 'brownfield'", status_code=422)
        return JSONResponse(status_code=422, content=resp.model_dump())

    # ─── Validate prompt ─────────────────────────────────────────────
    prompt = prompt.strip()
    if len(prompt) < PROMPT_MIN_LENGTH:
        resp = error_response(message="Prompt must not be empty", status_code=422)
        return JSONResponse(status_code=422, content=resp.model_dump())

    if len(prompt) > PROMPT_MAX_LENGTH:
        resp = error_response(message=f"Prompt must not exceed {PROMPT_MAX_LENGTH} characters", status_code=422)
        return JSONResponse(status_code=422, content=resp.model_dump())

    # ─── Brownfield: validate and upload files to Volume ─────────────
    volume_paths: list[str] = []

    if type == "brownfield":
        if not files:
            resp = error_response(
                message="At least one file is required for brownfield pipelines", status_code=422
            )
            return JSONResponse(status_code=422, content=resp.model_dump())

        try:
            volume_service = get_volume_upload_service()
            volume_paths = await volume_service.validate_and_upload(files)
        except FileValidationError as e:
            resp = error_response(message=e.message, status_code=422)
            return JSONResponse(status_code=422, content=resp.model_dump())
        except Exception as e:
            logger.exception(f"Volume upload failed: {e}")
            resp = error_response(
                message="Failed to upload files. Please try again later.", status_code=500
            )
            return JSONResponse(status_code=500, content=resp.model_dump())

    # ─── Build pipeline graph ────────────────────────────────────────
    logger.info(
        f"Starting {type} pipeline",
        extra={"prompt_length": len(prompt), "type": type, "file_count": len(volume_paths)},
    )

    try:
        graph = get_pipeline_graph()
    except Exception as e:
        logger.error(f"Failed to build pipeline graph: {e}")
        resp = error_response(message="Pipeline not available. Check dependencies.", status_code=503)
        return JSONResponse(status_code=503, content=resp.model_dump())

    # Initial state
    initial_state = {
        "messages": [],
        "prompt": prompt,
        "project_name": project_name or "",
        "pipeline_type": type,
        "volume_paths": volume_paths,
        "is_sufficient": True,
        "clarification_questions": [],
        "entities": [],
        "relationships": [],
        "prototype_context": "",
        "extracted_seed_data": {},
        "data_model": {},
        "schema_name": "",
        "table_names": [],
        "seed_row_counts": {},
        "backend_files": {},
        "frontend_files": {},
        "bundle_files": {},
        "bundle_valid": False,
        "app_name": "",
        "app_url": "",
        "service_principal_id": "",
        "deployment_tested": False,
        "deployment_error_logs": "",
        "deployment_retry_count": 0,
        "deployment_fix_context": "",
        "current_step": "",
        "completed_steps": [],
        "error": "",
    }

    async def event_stream():
        """Stream SSE events as the pipeline graph executes."""
        import time as _time

        pipeline_start = _time.time()
        project_id = None

        # Create audit record at pipeline start
        try:
            audit = get_audit_service()
            project_id = await audit.create_project_record(
                project_name=project_name or "unnamed-project",
                schema_name="",
                mode=type,
                prompt=prompt,
            )
            logger.info(f"Audit record created: {project_id}", extra={"step": "audit"})
        except Exception as e:
            logger.warning(f"Failed to create audit record: {e}")

        try:
            final_update = {}

            # Stream node-by-node updates
            async for event in graph.astream(initial_state, stream_mode="updates"):
                # event is a dict of {node_name: state_update}
                for node_name, update in event.items():
                    final_update = update  # Track last update for final state
                    current_step = update.get("current_step", node_name)
                    error = update.get("error", "")

                    # ─── Checkpoint SSE Extensions ────────────────────
                    # Detect checkpoint timeout (error contains "timed out")
                    if error and "timed out" in error.lower():
                        timeout_data = {
                            "step": current_step,
                            "status": "timeout",
                            "message": error,
                            "data": {
                                "checkpoint_type": current_step,
                                "timeout_seconds": CONFIRMATION_TIMEOUT_SECONDS,
                            },
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: confirmation_timeout\ndata: {json.dumps(timeout_data)}\n\n"
                        continue

                    # Detect awaiting_checkpoint (non-empty) → emit awaiting_confirmation
                    awaiting = update.get("awaiting_checkpoint", "")
                    if awaiting:
                        checkpoint_data = update.get("checkpoint_data", {})
                        awaiting_data = {
                            "step": current_step,
                            "status": "awaiting_confirmation",
                            "message": f"Awaiting user confirmation at checkpoint: {awaiting}",
                            "data": {
                                "checkpoint_type": awaiting,
                                "checkpoint_data": checkpoint_data,
                            },
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: awaiting_confirmation\ndata: {json.dumps(awaiting_data)}\n\n"
                        continue

                    # Detect confirmation received: awaiting_checkpoint cleared + user_corrections present
                    # This happens when a checkpoint node returns after user confirms
                    if (
                        "awaiting_checkpoint" in update
                        and update.get("awaiting_checkpoint") == ""
                        and update.get("user_corrections") is not None
                    ):
                        confirmed_data = {
                            "step": current_step,
                            "status": "confirmed",
                            "message": f"User confirmation received for checkpoint: {current_step}",
                            "data": {
                                "checkpoint_type": current_step,
                                "has_corrections": bool(update.get("user_corrections")),
                            },
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: confirmation_received\ndata: {json.dumps(confirmed_data)}\n\n"

                        resumed_data = {
                            "step": current_step,
                            "status": "resumed",
                            "message": f"Pipeline resumed after checkpoint: {current_step}",
                            "data": {
                                "checkpoint_type": current_step,
                                "next_step": current_step,
                            },
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: step_resumed\ndata: {json.dumps(resumed_data)}\n\n"

                        continue

                    # ─── Existing SSE Events (unchanged format) ───────
                    if error:
                        sse_data = {
                            "step": current_step,
                            "status": "failed",
                            "message": error,
                            "data": {"error_code": "STEP_FAILED"},
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: step_failed\ndata: {json.dumps(sse_data)}\n\n"
                    else:
                        step_data = _extract_step_data(node_name, update)
                        sse_data = {
                            "step": current_step,
                            "status": "completed",
                            "message": _step_message(node_name, update),
                            "data": step_data,
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"event: step_completed\ndata: {json.dumps(sse_data)}\n\n"

                        # ─── Pre-emit awaiting_confirmation for checkpoint nodes ───
                        # Since checkpoint nodes BLOCK graph.astream (they await asyncio.Event),
                        # we emit awaiting_confirmation immediately after the preceding step
                        # completes, using the step's output data as checkpoint_data.
                        _checkpoint_after = {
                            "intake": "analysis_review",
                            "brownfield_exploration": "analysis_review",
                        }
                        if node_name in _checkpoint_after:
                            checkpoint_type = _checkpoint_after[node_name]
                            # Build checkpoint data from the step's output
                            cp_data = {
                                "entities": update.get("entities", []),
                                "relationships": update.get("relationships", []),
                                "project_name": update.get("project_name", ""),
                            }
                            if node_name == "brownfield_exploration":
                                cp_data["project_structure"] = update.get("project_structure", {})
                                cp_data["tech_stack"] = update.get("tech_stack", {})

                            awaiting_data = {
                                "step": checkpoint_type,
                                "status": "awaiting_confirmation",
                                "message": f"Awaiting user confirmation at checkpoint: {checkpoint_type}",
                                "data": {
                                    "checkpoint_type": checkpoint_type,
                                    "checkpoint_data": cp_data,
                                },
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                            yield f"event: awaiting_confirmation\ndata: {json.dumps(awaiting_data)}\n\n"

            # Pipeline complete — use the last update to determine outcome
            pipeline_duration = _time.time() - pipeline_start

            if final_update.get("error"):
                # Update audit: failed
                if project_id:
                    try:
                        await audit.update_project_failed(
                            project_id=project_id,
                            failure_step=final_update.get("current_step", "unknown"),
                            failure_message=final_update.get("error", "")[:500],
                        )
                    except Exception:
                        pass

                complete_data = {
                    "step": None,
                    "status": "failed",
                    "message": f"Pipeline failed: {final_update['error']}",
                    "data": {
                        "error_code": "PIPELINE_FAILED",
                        "failed_step": final_update.get("current_step"),
                        "completed_steps": final_update.get("completed_steps", []),
                    },
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            elif final_update.get("is_sufficient") is False:
                complete_data = {
                    "step": None,
                    "status": "needs_clarification",
                    "message": "More information needed",
                    "data": {"clarification_questions": final_update.get("clarification_questions", [])},
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            else:
                # Update audit: completed
                if project_id:
                    try:
                        await audit.update_project_completed(
                            project_id=project_id,
                            app_name=final_update.get("app_name", ""),
                            app_url=final_update.get("app_url", ""),
                            service_principal_id=final_update.get("service_principal_id", ""),
                            tables_created=final_update.get("table_names", []),
                            pipeline_duration_seconds=pipeline_duration,
                            total_token_usage=0,
                            schema_name=final_update.get("schema_name", ""),
                        )
                    except Exception:
                        pass

                complete_data = {
                    "step": None,
                    "status": "completed",
                    "message": "App deployed successfully",
                    "data": {
                        "app_url": final_update.get("app_url", ""),
                        "app_name": final_update.get("app_name", ""),
                        "schema_name": final_update.get("schema_name", ""),
                        "catalog": "lakebase_accelerator_poc",
                        "tables_created": final_update.get("table_names", []),
                        "completed_steps": final_update.get("completed_steps", []),
                        "pipeline_duration_seconds": round(pipeline_duration, 1),
                    },
                    "timestamp": datetime.now(UTC).isoformat(),
                }

            yield f"event: pipeline_complete\ndata: {json.dumps(complete_data)}\n\n"

        except Exception as e:
            logger.exception(f"Pipeline error: {e}")

            # Update audit: failed
            if project_id:
                try:
                    await audit.update_project_failed(
                        project_id=project_id,
                        failure_step="pipeline_error",
                        failure_message=str(e)[:500],
                    )
                except Exception:
                    pass

            error_data = {
                "step": None,
                "status": "failed",
                "message": f"Pipeline error: {str(e)[:300]}",
                "data": {"error_code": "AGENT_ERROR"},
                "timestamp": datetime.now(UTC).isoformat(),
            }
            yield f"event: pipeline_complete\ndata: {json.dumps(error_data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════════════════════════
# PROJECT LIST & DETAIL (JSON)
# ═══════════════════════════════════════════════════════════════════════


@router.get("", response_model=ApiResponse)
async def list_projects(
    mode: ProjectMode | None = None,
    status: PipelineStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JSONResponse:
    """List all projects (chat history)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    try:
        audit = get_audit_service()
        rows, total = await audit.get_all_projects(
            mode=mode.value if mode else None,
            status=status.value if status else None,
            limit=limit,
            offset=offset,
        )
        projects = [
            ProjectSummary(
                id=str(row["id"]),
                project_name=row["project_name"],
                mode=ProjectMode(row["mode"]),
                status=PipelineStatus(row["status"]),
                prompt_preview=row["prompt"][:100] if row.get("prompt") else "",
                app_url=row.get("app_url"),
                schema_name=row.get("schema_name"),
                tables_created=row.get("generated_tables") if isinstance(row.get("generated_tables"), list) else None,
                created_at=row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
            )
            for row in rows
        ]
        data = ProjectListData(projects=projects, total=total, limit=limit, offset=offset)
        resp = success_response(message="Projects retrieved successfully", data=data.model_dump(mode='json'))
        return JSONResponse(status_code=200, content=resp.model_dump(mode='json'))
    except RuntimeError as e:
        logger.warning(f"RuntimeError in list_projects (likely pool not ready): {e}")
        data = ProjectListData(projects=[], total=0, limit=limit, offset=offset)
        resp = success_response(message="Projects retrieved successfully", data=data.model_dump(mode='json'))
        return JSONResponse(status_code=200, content=resp.model_dump(mode='json'))
    except Exception as e:
        logger.exception(f"Error listing projects: {e}")
        resp = error_response(message="An unexpected error occurred.", status_code=500)
        return JSONResponse(status_code=500, content=resp.model_dump())


@router.get("/{project_id}", response_model=ApiResponse)
async def get_project_detail(project_id: str) -> JSONResponse:
    """Get project detail."""
    try:
        audit = get_audit_service()
        row = await audit.get_project_detail(project_id)
    except RuntimeError as e:
        logger.warning(f"RuntimeError in get_project_detail: {e}")
        row = None
    except Exception as e:
        logger.exception(f"Error: {e}")
        resp = error_response(message="An unexpected error occurred.", status_code=500)
        return JSONResponse(status_code=500, content=resp.model_dump(mode='json'))

    if not row:
        resp = error_response(message=f"Project with ID {project_id} not found", status_code=404)
        return JSONResponse(status_code=404, content=resp.model_dump(mode='json'))

    from lakebase_accelerator.settings import get_settings as _get_settings

    settings = _get_settings()
    data = ProjectDetailData(
        id=str(row["id"]),
        project_name=row["project_name"],
        mode=ProjectMode(row["mode"]),
        status=PipelineStatus(row["status"]),
        prompt=row["prompt"],
        app_name=row.get("app_name"),
        app_url=row.get("app_url"),
        schema_name=row.get("schema_name"),
        catalog=settings.catalog_name if row.get("schema_name") else None,
        tables_created=row.get("generated_tables") or [],
        pipeline_duration_seconds=row.get("pipeline_duration_seconds"),
        total_token_usage=row.get("total_token_usage"),
        steps=[],
        created_at=row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        updated_at=(row.get("modified_at") or row["created_at"]).isoformat() if hasattr((row.get("modified_at") or row["created_at"]), "isoformat") else str(row.get("modified_at") or row["created_at"]),
    )
    resp = success_response(message="Project retrieved successfully", data=data.model_dump(mode='json'))
    return JSONResponse(status_code=200, content=resp.model_dump(mode='json'))


# ═══════════════════════════════════════════════════════════════════════
# CONFIRMATION ENDPOINT
# ═══════════════════════════════════════════════════════════════════════


@router.post("/{project_id}/confirm", response_model=ApiResponse)
async def confirm_checkpoint(project_id: str, body: ConfirmationRequest) -> JSONResponse:
    """Submit user confirmation for a pipeline checkpoint.

    Validates that the pipeline is awaiting confirmation and that the
    checkpoint_type matches the current pending checkpoint. Signals the
    waiting checkpoint node to resume via asyncio.Event.

    Returns:
        200: Confirmation submitted successfully.
        409: Pipeline is not awaiting confirmation or checkpoint_type mismatch.

    Requirements: 2.38, 2.39, 2.40, 2.41
    """
    # Check if the project is awaiting confirmation
    if not is_awaiting_confirmation(project_id):
        resp = error_response(
            message=f"Project '{project_id}' is not awaiting confirmation",
            status_code=409,
        )
        return JSONResponse(status_code=409, content=resp.model_dump())

    # Validate checkpoint_type matches the current pending checkpoint
    pending_type = get_pending_checkpoint(project_id)
    if pending_type != body.checkpoint_type:
        resp = error_response(
            message=(
                f"Checkpoint type mismatch: expected '{pending_type}', "
                f"got '{body.checkpoint_type}'"
            ),
            status_code=409,
        )
        return JSONResponse(status_code=409, content=resp.model_dump())

    # Signal the waiting checkpoint node to resume
    success = submit_confirmation(
        project_id=project_id,
        approved=body.approved,
        corrections=body.corrections,
        dismissed_items=body.dismissed_items,
        additional_context=body.additional_context,
    )

    if not success:
        resp = error_response(
            message=f"Failed to submit confirmation for project '{project_id}'",
            status_code=409,
        )
        return JSONResponse(status_code=409, content=resp.model_dump())

    resp = success_response(
        message="Confirmation submitted successfully",
        data={"project_id": project_id, "checkpoint_type": body.checkpoint_type, "approved": body.approved},
    )
    return JSONResponse(status_code=200, content=resp.model_dump())


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _format_heartbeat_event(checkpoint_type: str, elapsed_seconds: int) -> str:
    """Format a heartbeat SSE event.

    Heartbeat events are emitted every 30 seconds while the pipeline is awaiting
    user confirmation at a checkpoint. Since graph.astream blocks during checkpoint
    waits, heartbeats are emitted by the checkpoint node internally via logging.
    This helper formats the SSE event for cases where heartbeat emission is possible
    (e.g., when using an async wrapper around the checkpoint wait).

    Requirements: 2.42, 2.43
    """
    heartbeat_data = {
        "step": checkpoint_type,
        "status": "heartbeat",
        "message": f"Pipeline awaiting confirmation at checkpoint: {checkpoint_type}",
        "data": {
            "checkpoint_type": checkpoint_type,
            "elapsed_seconds": elapsed_seconds,
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return f"event: heartbeat\ndata: {json.dumps(heartbeat_data)}\n\n"


def _extract_step_data(node_name: str, update: dict) -> dict:
    """Extract relevant data from a node update for SSE."""
    if node_name in ("intake", "prototype_ingestion"):
        return {"entities": len(update.get("entities", [])), "relationships": len(update.get("relationships", []))}
    if node_name == "prototype_analysis":
        seed_data = update.get("extracted_seed_data", {})
        return {"seed_tables": len(seed_data), "context_length": len(update.get("prototype_context", ""))}
    if node_name == "data_model":
        dm = update.get("data_model", {})
        return {"tables": len(dm.get("tables", []))}
    if node_name == "schema_provisioning":
        return {"schema_name": update.get("schema_name", ""), "tables": update.get("table_names", [])}
    if node_name == "seed_data":
        return {"row_counts": update.get("seed_row_counts", {})}
    if node_name == "backend_dev":
        return {"files": len(update.get("backend_files", {}))}
    if node_name == "frontend_dev":
        return {"files": len(update.get("frontend_files", {}))}
    if node_name == "integration":
        return {"total_files": len(update.get("bundle_files", {})), "valid": update.get("bundle_valid", False)}
    if node_name == "deployment":
        return {"app_url": update.get("app_url", ""), "app_name": update.get("app_name", "")}
    return {}


def _step_message(node_name: str, update: dict) -> str:
    """Generate a human-readable message for a completed step."""
    messages = {
        "intake": f"Analyzed prompt: {len(update.get('entities', []))} entities found",
        "prototype_ingestion": f"Reverse-engineered data model: {len(update.get('entities', []))} entities found",
        "prototype_analysis": f"Analyzed prototype: extracted seed data and UI context",
        "data_model": f"Designed data model: {len(update.get('data_model', {}).get('tables', []))} tables",
        "schema_provisioning": f"Created schema: {update.get('schema_name', '')}",
        "seed_data": f"Inserted seed data: {sum(update.get('seed_row_counts', {}).values())} rows",
        "backend_dev": f"Generated backend: {len(update.get('backend_files', {}))} files",
        "frontend_dev": f"Generated frontend: {len(update.get('frontend_files', {}))} files",
        "integration": f"Bundle ready: {len(update.get('bundle_files', {}))} files",
        "deployment": f"Deployed: {update.get('app_url', '')}",
    }
    return messages.get(node_name, f"Completed: {node_name}")
