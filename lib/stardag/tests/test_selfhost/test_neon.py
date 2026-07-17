"""Tests for the Neon provisioning client (mocked HTTP)."""

import json

import httpx
import pytest

from stardag.selfhost._neon import (
    NeonAuthError,
    NeonClient,
    to_sqlalchemy_asyncpg_url,
)


def test_to_sqlalchemy_asyncpg_url_strips_libpq_params():
    uri = (
        "postgres://user:pw@ep-abc-123.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    assert to_sqlalchemy_asyncpg_url(uri) == (
        "postgresql+asyncpg://user:pw@ep-abc-123.eu-central-1.aws.neon.tech"
        "/neondb?ssl=require"
    )


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_get_or_create_project_existing():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path == "/api/v2/projects" and request.method == "GET":
            return httpx.Response(
                200, json={"projects": [{"id": "proj-1", "name": "stardag"}]}
            )
        if path == "/api/v2/projects/proj-1/branches":
            return httpx.Response(
                200, json={"branches": [{"id": "br-1", "default": True}]}
            )
        if path == "/api/v2/projects/proj-1/branches/br-1/databases":
            return httpx.Response(
                200,
                json={"databases": [{"name": "neondb", "owner_name": "neondb_owner"}]},
            )
        if path == "/api/v2/projects/proj-1/connection_uri":
            pooled = request.url.params["pooled"] == "true"
            host = "ep-1-pooler.neon.tech" if pooled else "ep-1.neon.tech"
            return httpx.Response(
                200, json={"uri": f"postgres://u:p@{host}/neondb?sslmode=require"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    client = NeonClient("key", transport=_mock_transport(handler))
    db = client.get_or_create_project("stardag")
    assert db.project_id == "proj-1"
    assert not db.created
    assert "ep-1.neon.tech" in db.direct_uri
    assert "ep-1-pooler.neon.tech" in db.pooled_uri
    # No POST /projects call for an existing project
    assert "POST /api/v2/projects" not in calls


def test_get_or_create_project_creates_with_pg16():
    created_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/projects" and request.method == "GET":
            return httpx.Response(200, json={"projects": []})
        if path == "/api/v2/projects" and request.method == "POST":
            created_payload.update(json.loads(request.content))
            return httpx.Response(
                201, json={"project": {"id": "proj-new", "name": "stardag"}}
            )
        if path == "/api/v2/projects/proj-new/branches":
            return httpx.Response(
                200, json={"branches": [{"id": "br-1", "default": True}]}
            )
        if path == "/api/v2/projects/proj-new/branches/br-1/databases":
            return httpx.Response(
                200,
                json={"databases": [{"name": "neondb", "owner_name": "neondb_owner"}]},
            )
        if path == "/api/v2/projects/proj-new/connection_uri":
            return httpx.Response(
                200, json={"uri": "postgres://u:p@ep-2.neon.tech/neondb"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    client = NeonClient("key", transport=_mock_transport(handler))
    db = client.get_or_create_project("stardag")
    assert db.created
    assert created_payload == {"project": {"name": "stardag", "pg_version": 16}}


def test_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client = NeonClient("bad-key", transport=_mock_transport(handler))
    with pytest.raises(NeonAuthError):
        client.get_or_create_project("stardag")
