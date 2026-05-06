"""Tests for project endpoints — greenfield, brownfield, list, detail."""

import pytest
from fastapi.testclient import TestClient

from lakebase_accelerator.main import app


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


class TestGreenfieldEndpoint:
    """Tests for POST /api/v1/projects/greenfield."""

    def test_greenfield_returns_sse_or_503(self, client):
        """Valid prompt returns SSE stream or 503 if deps not ready."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": "Build a task management app"},
        )
        # Either SSE stream (200) or service unavailable (503)
        assert response.status_code in (200, 503)
        if response.status_code == 503:
            data = response.json()
            assert data["success"] is False
            assert data["statusCode"] == 503
            assert data["data"] is None

    def test_greenfield_rejects_empty_prompt(self, client):
        """Empty prompt returns 422 with standard envelope."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": ""},
        )
        assert response.status_code == 422
        data = response.json()
        assert data["success"] is False
        assert data["statusCode"] == 422
        assert data["data"] is None

    def test_greenfield_rejects_whitespace_prompt(self, client):
        """Whitespace-only prompt returns 422."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": "   "},
        )
        assert response.status_code == 422

    def test_greenfield_rejects_oversized_prompt(self, client):
        """Prompt exceeding 10000 chars returns 422."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": "x" * 10_001},
        )
        assert response.status_code == 422

    def test_greenfield_accepts_valid_project_name(self, client):
        """Valid kebab-case project name is accepted."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": "Build an app", "project_name": "my-cool-app"},
        )
        assert response.status_code in (200, 503)

    def test_greenfield_rejects_invalid_project_name(self, client):
        """Non-kebab-case project name returns 422."""
        response = client.post(
            "/api/v1/projects/greenfield",
            json={"prompt": "Build an app", "project_name": "My App!"},
        )
        assert response.status_code == 422


class TestProjectListEndpoint:
    """Tests for GET /api/v1/projects."""

    def test_list_projects_returns_standard_envelope(self, client):
        """Project list returns standard response envelope."""
        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["statusCode"] == 200
        assert "data" in data
        assert "projects" in data["data"]
        assert "total" in data["data"]
        assert "limit" in data["data"]
        assert "offset" in data["data"]


class TestProjectDetailEndpoint:
    """Tests for GET /api/v1/projects/{project_id}."""

    def test_project_not_found_returns_404(self, client):
        """Non-existent project returns 404 with standard envelope."""
        response = client.get("/api/v1/projects/550e8400-e29b-41d4-a716-446655440000")
        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert data["statusCode"] == 404
        assert "not found" in data["message"]
        assert data["data"] is None
