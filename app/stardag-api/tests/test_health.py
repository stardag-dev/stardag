"""Smoke test for API health endpoint."""

from fastapi.testclient import TestClient

from stardag_api.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
