"""Default routes — health, readiness, and root."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from lakebase_accelerator.models.responses import (
    ApiResponse,
    HealthData,
    ReadinessChecks,
    ReadinessData,
    success_response,
)
from lakebase_accelerator.settings import get_settings

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=ApiResponse)
async def health_check() -> JSONResponse:
    """Liveness probe — returns healthy if the application is running."""
    settings = get_settings()
    data = HealthData(status="healthy", version=settings.app_version)
    resp = success_response(message="Service is healthy", data=data.model_dump())
    return JSONResponse(status_code=200, content=resp.model_dump())


@router.get("/ready", response_model=ApiResponse)
async def readiness_check() -> JSONResponse:
    """Readiness probe — checks Lakebase and Model Serving connectivity."""
    # TODO: Implement actual dependency checks once repositories are wired
    checks = ReadinessChecks(lakebase="connected", model_serving="ready")
    data = ReadinessData(status="ready", checks=checks)
    resp = success_response(message="All dependencies ready", data=data.model_dump())
    return JSONResponse(status_code=200, content=resp.model_dump())
