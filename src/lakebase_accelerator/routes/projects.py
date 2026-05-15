"""Project routes — pipeline execution, SSE events, list, and detail endpoints.

Architecture:
- POST /execute → validates input, starts pipeline as background asyncio.Task,
  returns project_id immediately.
- GET /projects/{id}/events → reconnectable SSE stream that reads from an
  in-memory event store. If the Databricks Apps proxy drops the connection
  (~5 min hard timeout), the frontend reconnects with Last-Event-ID and
  picks up where it left off. The pipeline continues running regardless.
"""

import asyncio
import json
import time as _time
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
from lakebase_accelerator.services.pipeline_event_store import (
    PipelineRun,
    create_pipeline_run,
    get_pipeline_run,
)
from lakebase_accelerator.services.volume_upload_service import FileValidationError
from lakebase_accelerator.settings import PROMPT_MAX_LENGTH, PROMPT_MIN_LENGTH
from lakebase_accelerator.utils.logger import logger

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


def _log_task_exception(task: asyncio.Task) -> None:
    """Callback to log exceptions from background tasks that would otherwise be swallowed."""
    try:
        exc = task.exception()
        if exc:
            logger.error(
                f"Background pipeline task failed with exception: {exc}",
                exc_info=exc,
                extra={"step": "pipeline_bg"},
            )
    except asyncio.CancelledError:
        logger.warning("Background pipeline task was cancelled", extra={"step": "pipeline_bg"})


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE EXECUTION (Background Task)
# ═══════════════════════════════════════════════════════════════════════


@router.post("/execute", response_model=None)
async def execute_pipeline(
    type: str = Form(...),
    prompt: str = Form(...),
    files: list[UploadFile] | None = File(None),
) -> StreamingResponse | JSONResponse:
    """Start the accelerator pipeline as a background task.

    Accepts multipart/form-data for both greenfield and brownfield:
    - Greenfield: type=greenfield, prompt=<text> (no files needed)
    - Brownfield: type=brownfield, prompt=<text>, files=<binary uploads>

    Returns an SSE stream with real-time progress. The pipeline runs as a
    background task, so if the connection drops (Databricks Apps ~5 min proxy
    timeout), the frontend can reconnect via GET /projects/{id}/events?last_event_id=N
    to resume receiving events without losing progress.
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

    # ─── Create audit record ─────────────────────────────────────────
    project_id = None
    try:
        audit = get_audit_service()
        project_id = await audit.create_project_record(
            project_name="",
            schema_name="",
            mode=type,
            prompt=prompt,
            uploaded_files=[
                {"name": path.rsplit("/", 1)[-1], "volume_path": path}
                for path in volume_paths
            ] if volume_paths else None,
        )
        logger.info(f"Audit record created: {project_id}", extra={"step": "audit"})

        # Save user prompt as first chat message
        if project_id:
            await audit.append_chat_message(
                project_id=project_id,
                role="user",
                content=prompt,
                message_type="message",
            )
    except Exception as e:
        logger.warning(f"Failed to create audit record: {e}")

    if not project_id:
        # Generate a temporary ID if audit fails
        import uuid
        project_id = str(uuid.uuid4())

    # ─── Create event store and start background task ────────────────
    pipeline_run = create_pipeline_run(project_id)

    # Initial state
    initial_state = {
        "messages": [],
        "prompt": prompt,
        "project_name": "",
        "project_id": project_id,
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
        "theme": {"mode": "dark", "brand_color": "#3b82f6", "brand_name": "blue"},
        "layout": "sidebar",
    }

    # Launch pipeline in background — runs independently of HTTP connection
    task = asyncio.create_task(
        _run_pipeline_background(graph, initial_state, pipeline_run, type, project_id, prompt, volume_paths)
    )
    # Log unhandled exceptions from the background task (otherwise they're silently swallowed)
    task.add_done_callback(_log_task_exception)

    # Return SSE stream that reads from the event store (backward compatible with frontend)
    # The pipeline runs in the background; this stream just relays events.
    # If the proxy kills this connection, the frontend can reconnect via
    # GET /projects/{project_id}/events?last_event_id=N
    HEARTBEAT_INTERVAL = 15  # seconds
    stream_start = _time.time()

    async def event_stream():
        """Relay events from background pipeline to SSE stream."""
        cursor = 0

        while True:
            new_events = [e for e in pipeline_run.events if e.event_id > cursor]

            for event in new_events:
                cursor = event.event_id
                yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {json.dumps(event.data)}\n\n"

                if event.event_type == "pipeline_complete":
                    return

            if pipeline_run.is_complete:
                return

            try:
                await asyncio.wait_for(
                    _wait_for_notify(pipeline_run),
                    timeout=HEARTBEAT_INTERVAL,
                )
            except asyncio.TimeoutError:
                heartbeat_data = {
                    "step": "processing",
                    "status": "heartbeat",
                    "message": "Pipeline is processing...",
                    "data": {
                        "elapsed_seconds": round(_time.time() - stream_start),
                        "project_id": project_id,
                    },
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                yield f"id: 0\nevent: heartbeat\ndata: {json.dumps(heartbeat_data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "Content-Encoding": "identity",
        },
    )


async def _run_pipeline_background(
    graph,
    initial_state: dict,
    pipeline_run: PipelineRun,
    pipeline_type: str,
    project_id: str,
    prompt: str,
    volume_paths: list[str],
) -> None:
    """Run the pipeline graph as a background task, pushing events to the store.

    This function runs independently of any HTTP connection. Events are stored
    in-memory and consumed by the SSE endpoint when clients connect.
    """
    logger.info(f"Background pipeline task started: {project_id}", extra={"step": "pipeline_bg"})
    pipeline_start = _time.time()

    # Accumulate key data from steps
    accumulated_schema_name = ""
    accumulated_table_names: list[str] = []
    accumulated_app_url = ""
    accumulated_app_name = ""
    accumulated_project_name = ""
    final_update = {}

    try:
        async for event in graph.astream(initial_state, stream_mode="updates"):
            for node_name, update in event.items():
                logger.info(
                    f"Pipeline node completed: {node_name}",
                    extra={"step": "pipeline_bg", "project_id": project_id, "node": node_name},
                )
                # Accumulate data from specific steps
                if update.get("schema_name"):
                    accumulated_schema_name = update["schema_name"]
                if update.get("table_names"):
                    accumulated_table_names = update["table_names"]
                if update.get("app_url"):
                    accumulated_app_url = update["app_url"]
                if update.get("app_name"):
                    accumulated_app_name = update["app_name"]
                if update.get("project_name"):
                    accumulated_project_name = update["project_name"]
                final_update = update
                current_step = update.get("current_step", node_name)
                error = update.get("error", "")

                # ─── Checkpoint SSE Extensions ────────────────────
                if error and "timed out" in error.lower():
                    pipeline_run.push_event("confirmation_timeout", {
                        "step": current_step,
                        "status": "timeout",
                        "message": error,
                        "data": {
                            "checkpoint_type": current_step,
                            "timeout_seconds": CONFIRMATION_TIMEOUT_SECONDS,
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                    continue

                # Detect awaiting_checkpoint
                awaiting = update.get("awaiting_checkpoint", "")
                if awaiting:
                    checkpoint_data = update.get("checkpoint_data", {})
                    pipeline_run.push_event("awaiting_confirmation", {
                        "step": current_step,
                        "status": "awaiting_confirmation",
                        "message": f"Awaiting user confirmation at checkpoint: {awaiting}",
                        "data": {
                            "checkpoint_type": awaiting,
                            "checkpoint_data": checkpoint_data,
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                    continue

                # Detect confirmation received
                if (
                    "awaiting_checkpoint" in update
                    and update.get("awaiting_checkpoint") == ""
                    and update.get("user_corrections") is not None
                ):
                    pipeline_run.push_event("confirmation_received", {
                        "step": current_step,
                        "status": "confirmed",
                        "message": f"User confirmation received for checkpoint: {current_step}",
                        "data": {
                            "checkpoint_type": current_step,
                            "has_corrections": bool(update.get("user_corrections")),
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    })

                    # Save to chat history
                    try:
                        audit = get_audit_service()
                        corrections = update.get("user_corrections", {})
                        if corrections:
                            user_feedback = corrections.get("user_feedback", str(corrections))
                            await audit.append_chat_message(
                                project_id=project_id,
                                role="user",
                                content=user_feedback,
                                message_type="checkpoint_correction",
                            )
                        else:
                            await audit.append_chat_message(
                                project_id=project_id,
                                role="user",
                                content="Approved",
                                message_type="checkpoint_approval",
                            )
                    except Exception:
                        pass

                    pipeline_run.push_event("step_resumed", {
                        "step": current_step,
                        "status": "resumed",
                        "message": f"Pipeline resumed after checkpoint: {current_step}",
                        "data": {
                            "checkpoint_type": current_step,
                            "next_step": current_step,
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                    continue

                # ─── Standard step events ────────────────────────
                if error:
                    pipeline_run.push_event("step_failed", {
                        "step": current_step,
                        "status": "failed",
                        "message": error,
                        "data": {"error_code": "STEP_FAILED"},
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
                else:
                    step_data = _extract_step_data(node_name, update)
                    pipeline_run.push_event("step_completed", {
                        "step": current_step,
                        "status": "completed",
                        "message": _step_message(node_name, update),
                        "data": step_data,
                        "timestamp": datetime.now(UTC).isoformat(),
                    })

                    # Pre-emit awaiting_confirmation for checkpoint nodes
                    _checkpoint_after = {
                        "intake": "analysis_review",
                        "brownfield_exploration": "analysis_review",
                    }
                    if node_name in _checkpoint_after:
                        checkpoint_type = _checkpoint_after[node_name]
                        checkpoint_project_name = update.get("project_name", accumulated_project_name)

                        summary_md = _generate_checkpoint_summary(
                            entities=update.get("entities", []),
                            relationships=update.get("relationships", []),
                            tech_stack=update.get("tech_stack", {}),
                            project_name=checkpoint_project_name,
                            pipeline_type=pipeline_type,
                        )

                        pipeline_run.push_event("awaiting_confirmation", {
                            "step": checkpoint_type,
                            "status": "awaiting_confirmation",
                            "message": f"Awaiting user confirmation at checkpoint: {checkpoint_type}",
                            "project_id": project_id,
                            "project_name": checkpoint_project_name,
                            "data": {
                                "checkpoint_type": checkpoint_type,
                                "summary": summary_md,
                            },
                            "timestamp": datetime.now(UTC).isoformat(),
                        })

                        # Save checkpoint summary to chat history
                        try:
                            audit = get_audit_service()
                            await audit.append_chat_message(
                                project_id=project_id,
                                role="agent",
                                content=summary_md,
                                message_type="checkpoint_summary",
                            )
                        except Exception:
                            pass

        # ─── Pipeline complete ───────────────────────────────────────
        pipeline_duration = _time.time() - pipeline_start

        if final_update.get("error"):
            try:
                audit = get_audit_service()
                await audit.update_project_failed(
                    project_id=project_id,
                    failure_step=final_update.get("current_step", "unknown"),
                    failure_message=final_update.get("error", "")[:500],
                )
            except Exception:
                pass

            pipeline_run.push_event("pipeline_complete", {
                "step": None,
                "status": "failed",
                "message": f"Pipeline failed: {final_update['error']}",
                "data": {
                    "error_code": "PIPELINE_FAILED",
                    "failed_step": final_update.get("current_step"),
                    "completed_steps": final_update.get("completed_steps", []),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            })
        elif final_update.get("is_sufficient") is False:
            pipeline_run.push_event("pipeline_complete", {
                "step": None,
                "status": "needs_clarification",
                "message": "More information needed",
                "data": {"clarification_questions": final_update.get("clarification_questions", [])},
                "timestamp": datetime.now(UTC).isoformat(),
            })
        else:
            try:
                audit = get_audit_service()
                await audit.update_project_completed(
                    project_id=project_id,
                    app_name=accumulated_app_name or final_update.get("app_name", ""),
                    app_url=accumulated_app_url or final_update.get("app_url", ""),
                    tables_created=accumulated_table_names or final_update.get("table_names", []),
                    pipeline_duration_seconds=pipeline_duration,
                    schema_name=accumulated_schema_name or final_update.get("schema_name", ""),
                    project_name=accumulated_project_name,
                )
                logger.info(
                    f"Audit updated to completed: {project_id}, app_url={accumulated_app_url or final_update.get('app_url', '')}",
                    extra={"step": "pipeline_bg"},
                )
            except Exception as e:
                logger.error(f"Failed to update audit to completed: {e}", extra={"step": "pipeline_bg"})

            pipeline_run.push_event("pipeline_complete", {
                "step": None,
                "status": "completed",
                "message": "App deployed successfully",
                "data": {
                    "app_url": accumulated_app_url or final_update.get("app_url", ""),
                    "app_name": accumulated_app_name or final_update.get("app_name", ""),
                    "schema_name": accumulated_schema_name or final_update.get("schema_name", ""),
                    "catalog": "lakebase_accelerator_poc",
                    "tables_created": accumulated_table_names or final_update.get("table_names", []),
                    "completed_steps": final_update.get("completed_steps", []),
                    "pipeline_duration_seconds": round(pipeline_duration, 1),
                },
                "timestamp": datetime.now(UTC).isoformat(),
            })
            logger.info(f"Pipeline complete event pushed for: {project_id}", extra={"step": "pipeline_bg"})

    except Exception as e:
        logger.exception(f"Pipeline background task error: {e}")
        try:
            audit = get_audit_service()
            await audit.update_project_failed(
                project_id=project_id,
                failure_step="pipeline_error",
                failure_message=str(e)[:500],
            )
        except Exception:
            pass

        pipeline_run.push_event("pipeline_complete", {
            "step": None,
            "status": "failed",
            "message": f"Pipeline error: {str(e)[:300]}",
            "data": {"error_code": "AGENT_ERROR"},
            "timestamp": datetime.now(UTC).isoformat(),
        })
    finally:
        pipeline_run.mark_complete()
        logger.info(
            f"Background pipeline task finished: {project_id} (duration: {round(_time.time() - pipeline_start, 1)}s)",
            extra={"step": "pipeline_bg"},
        )


# ═══════════════════════════════════════════════════════════════════════
# SSE EVENT STREAM (Reconnectable)
# ═══════════════════════════════════════════════════════════════════════


@router.get("/{project_id}/events", response_model=None)
async def stream_pipeline_events(
    project_id: str,
    last_event_id: int = 0,
) -> StreamingResponse | JSONResponse:
    """Stream SSE events for a running pipeline. Supports reconnection.

    Query params:
        last_event_id: Resume from this event ID (0 = start from beginning).
                       On reconnect, pass the last received event ID.

    The stream emits:
    - event: step_completed / step_failed / awaiting_confirmation / etc.
    - event: heartbeat (every 15s to keep connection alive)
    - event: pipeline_complete (terminal event)

    If the connection drops (Databricks Apps ~5 min timeout), the frontend
    reconnects with ?last_event_id=N and picks up where it left off.
    """
    pipeline_run = get_pipeline_run(project_id)

    if not pipeline_run:
        resp = error_response(
            message=f"No active pipeline found for project '{project_id}'",
            status_code=404,
        )
        return JSONResponse(status_code=404, content=resp.model_dump())

    HEARTBEAT_INTERVAL = 15  # seconds
    stream_start = _time.time()

    async def event_stream():
        """Yield SSE events from the pipeline run's event store."""
        cursor = last_event_id  # Start from where the client left off

        while True:
            # Emit any events we haven't sent yet (replay on reconnect)
            new_events = [e for e in pipeline_run.events if e.event_id > cursor]

            for event in new_events:
                cursor = event.event_id
                yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {json.dumps(event.data)}\n\n"

                # If this is the terminal event, stop
                if event.event_type == "pipeline_complete":
                    return

            # If pipeline is done and we've sent all events, stop
            if pipeline_run.is_complete:
                return

            # Wait for new events or emit heartbeat
            try:
                await asyncio.wait_for(
                    _wait_for_notify(pipeline_run),
                    timeout=HEARTBEAT_INTERVAL,
                )
            except asyncio.TimeoutError:
                # No new events — emit heartbeat to keep connection alive
                heartbeat_data = {
                    "step": "processing",
                    "status": "heartbeat",
                    "message": "Pipeline is processing...",
                    "data": {"elapsed_seconds": round(_time.time() - stream_start)},
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat_data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "Content-Encoding": "identity",
        },
    )


async def _wait_for_notify(pipeline_run: PipelineRun) -> None:
    """Wait until the pipeline run pushes a new event."""
    # We poll the event count since asyncio.Event.clear() is called immediately
    initial_count = len(pipeline_run.events)
    while len(pipeline_run.events) == initial_count and not pipeline_run.is_complete:
        await asyncio.sleep(0.1)


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
        uploaded_files=row.get("uploaded_files") or [],
        chat_history=row.get("chat_history") or [],
        pipeline_duration_seconds=row.get("pipeline_duration_seconds"),
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

    The project_id is the UUID returned in the awaiting_confirmation SSE event.

    Returns:
        200: Confirmation submitted successfully.
        409: Pipeline is not awaiting confirmation or checkpoint_type mismatch.
    """
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
    """Format a heartbeat SSE event."""
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


def _generate_checkpoint_summary(
    entities: list[dict],
    relationships: list[dict],
    tech_stack: dict,
    project_name: str,
    pipeline_type: str,
) -> str:
    """Generate a business-level markdown summary for the checkpoint approval UI."""
    from lakebase_accelerator.agent.llm import get_llm

    entity_lines = []
    for e in entities:
        attrs = e.get("attributes", [])
        attr_names = [a["name"] for a in attrs if a["name"] not in ("id", "created_at", "updated_at")]
        entity_lines.append(f"- {e.get('name', 'unnamed')}: {e.get('description', '')} (fields: {', '.join(attr_names)})")

    rel_lines = []
    for r in relationships:
        rel_lines.append(f"- {r.get('from_entity', '')} → {r.get('to_entity', '')} ({r.get('cardinality', '')})")

    tech_info = ""
    if tech_stack:
        tech_parts = [f"{k}: {v}" for k, v in tech_stack.items() if v and k in ("language", "framework", "frontend", "orm")]
        tech_info = ", ".join(tech_parts)

    display_name = project_name.replace("-", " ").replace("_", " ").title() if project_name else "Your Application"

    prompt = f"""Generate a business-level summary for a user to review before we build their application.

## Context
- Application: {display_name}
- Pipeline: {pipeline_type}
- Tech Stack: {tech_info or "Not specified"}

## Discovered Entities
{chr(10).join(entity_lines)}

## Relationships
{chr(10).join(rel_lines) if rel_lines else "None"}

## Instructions
Write a markdown summary that reads like a project brief — NOT a technical schema review. Include:

1. **Application Overview** — What this app does in 2-3 sentences (from the user's perspective, not developer's)
2. **Key Features** — Bullet list of what the app will support (based on entities and relationships)
3. **What I'll Build** — Brief description of what will be generated:
   - Database with {len(entities)} tables
   - REST API with full CRUD endpoints
   - React frontend with the UI
   - Deployment to Databricks Apps
4. End with: "Click **Approve & Continue** to proceed, or edit the summary to make changes."

Keep it conversational and concise. Focus on WHAT the app does for the user, not HOW the database is structured.
Do NOT list column names or data types. Do NOT say "schema review".
Return ONLY the markdown, no code fences."""

    try:
        llm = get_llm(max_tokens=2048)
        response = llm.invoke(prompt)
        summary = response.content.strip()
        if summary.startswith("```"):
            summary = summary.split("\n", 1)[1] if "\n" in summary else summary[3:]
        if summary.endswith("```"):
            summary = summary[:-3].rstrip()
        return summary
    except Exception as e:
        logger.warning(f"LLM summary generation failed, using fallback: {e}")
        return _fallback_checkpoint_summary(entities, relationships, tech_stack, display_name)


def _fallback_checkpoint_summary(
    entities: list[dict],
    relationships: list[dict],
    tech_stack: dict,
    display_name: str,
) -> str:
    """Fallback summary when LLM call fails."""
    parts = [f"## {display_name}\n"]
    parts.append("Here's what I'm planning to build:\n")

    parts.append("**Key Features:**\n")
    for e in entities:
        name = e.get("name", "unnamed").replace("_", " ").title()
        desc = e.get("description", "")
        if desc:
            parts.append(f"- {desc}")
        else:
            parts.append(f"- {name} management")

    parts.append(f"\n**What I'll generate:**")
    parts.append(f"- Database with {len(entities)} tables and {len(relationships)} relationships")
    parts.append(f"- REST API with full CRUD endpoints")
    parts.append(f"- React frontend UI")
    parts.append(f"- Deployment to Databricks Apps")

    if tech_stack:
        framework = tech_stack.get("framework", "")
        language = tech_stack.get("language", "")
        if framework or language:
            parts.append(f"\n*Detected tech: {' + '.join(filter(None, [framework, language]))}*")

    parts.append("\n---\nClick **Approve & Continue** to proceed, or edit the summary to make changes.")
    return "\n".join(parts)
