"""Tests for health and readiness endpoints."""

import pytest
from fastapi.testclient import TestClient

from lakebase_accelerator.main import app


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


def test_health_check_returns_200(client):
    """GET /health returns standard envelope with healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["statusCode"] == 200
    assert data["message"] == "Service is healthy"
    assert data["data"]["status"] == "healthy"
    assert data["data"]["version"] == "1.0.0"


def test_readiness_check_returns_200(client):
    """GET /ready returns standard envelope with dependency checks."""
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["statusCode"] == 200
    assert data["data"]["status"] == "ready"
    assert data["data"]["checks"]["lakebase"] == "connected"
    assert data["data"]["checks"]["model_serving"] == "ready"
