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


class TestConfirmCheckpointEndpoint:
    """Tests for POST /api/v1/projects/{project_id}/confirm."""

    def test_confirm_returns_409_when_not_awaiting(self, client):
        """Returns 409 when project is not awaiting confirmation."""
        response = client.post(
            "/api/v1/projects/some-project-id/confirm",
            json={
                "checkpoint_type": "structure_review",
                "approved": True,
                "corrections": {},
                "additional_context": "",
                "dismissed_items": [],
            },
        )
        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert data["statusCode"] == 409
        assert "not awaiting confirmation" in data["message"]

    def test_confirm_returns_409_on_checkpoint_type_mismatch(self, client):
        """Returns 409 when checkpoint_type doesn't match pending checkpoint."""
        import asyncio

        from lakebase_accelerator.agent.nodes.checkpoint import _pending_confirmations

        # Simulate a pending checkpoint
        _pending_confirmations["test-project-mismatch"] = {
            "event": asyncio.Event(),
            "response": {},
            "checkpoint_type": "entity_review",
        }

        try:
            response = client.post(
                "/api/v1/projects/test-project-mismatch/confirm",
                json={
                    "checkpoint_type": "structure_review",
                    "approved": True,
                    "corrections": {},
                    "additional_context": "",
                    "dismissed_items": [],
                },
            )
            assert response.status_code == 409
            data = response.json()
            assert data["success"] is False
            assert data["statusCode"] == 409
            assert "mismatch" in data["message"].lower()
        finally:
            _pending_confirmations.pop("test-project-mismatch", None)

    def test_confirm_returns_200_on_valid_confirmation(self, client):
        """Returns 200 when confirmation is valid and submitted successfully."""
        import asyncio

        from lakebase_accelerator.agent.nodes.checkpoint import _pending_confirmations

        # Simulate a pending checkpoint
        _pending_confirmations["test-project-valid"] = {
            "event": asyncio.Event(),
            "response": {},
            "checkpoint_type": "structure_review",
        }

        try:
            response = client.post(
                "/api/v1/projects/test-project-valid/confirm",
                json={
                    "checkpoint_type": "structure_review",
                    "approved": True,
                    "corrections": {},
                    "additional_context": "Looks good",
                    "dismissed_items": [],
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["statusCode"] == 200
            assert "submitted successfully" in data["message"].lower()
            assert data["data"]["project_id"] == "test-project-valid"
            assert data["data"]["checkpoint_type"] == "structure_review"
            assert data["data"]["approved"] is True
        finally:
            _pending_confirmations.pop("test-project-valid", None)

    def test_confirm_with_corrections(self, client):
        """Returns 200 when confirmation includes corrections."""
        import asyncio

        from lakebase_accelerator.agent.nodes.checkpoint import _pending_confirmations

        _pending_confirmations["test-project-corrections"] = {
            "event": asyncio.Event(),
            "response": {},
            "checkpoint_type": "entity_review",
        }

        corrections = {"entities": [{"name": "User", "attributes": ["id", "email"]}]}

        try:
            response = client.post(
                "/api/v1/projects/test-project-corrections/confirm",
                json={
                    "checkpoint_type": "entity_review",
                    "approved": False,
                    "corrections": corrections,
                    "additional_context": "Please fix the User entity",
                    "dismissed_items": ["relationship-1"],
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["data"]["approved"] is False
        finally:
            _pending_confirmations.pop("test-project-corrections", None)

    def test_confirm_missing_required_fields(self, client):
        """Returns 422 when required fields are missing."""
        response = client.post(
            "/api/v1/projects/some-project/confirm",
            json={},
        )
        assert response.status_code == 422

    def test_confirm_signals_event(self, client):
        """Confirm endpoint sets the asyncio.Event to unblock the waiting node."""
        import asyncio

        from lakebase_accelerator.agent.nodes.checkpoint import _pending_confirmations

        event = asyncio.Event()
        _pending_confirmations["test-project-event"] = {
            "event": event,
            "response": {},
            "checkpoint_type": "analysis_review",
        }

        try:
            response = client.post(
                "/api/v1/projects/test-project-event/confirm",
                json={
                    "checkpoint_type": "analysis_review",
                    "approved": True,
                    "corrections": {},
                    "additional_context": "",
                    "dismissed_items": [],
                },
            )
            assert response.status_code == 200
            # Verify the event was set
            assert event.is_set()
        finally:
            _pending_confirmations.pop("test-project-event", None)
