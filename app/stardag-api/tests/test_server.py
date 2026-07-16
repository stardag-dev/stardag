"""Tests for the version endpoint and the combined API + UI server module."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from stardag_api.server import UI_MOUNT_NAME, create_app, mount_ui


@pytest.mark.asyncio
async def test_version_endpoint_defaults(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("STARDAG_SERVER_VERSION", raising=False)
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    data = response.json()
    assert data["server_version"] == "dev"
    # Installed package version (whatever it is, it must be non-empty)
    assert data["api_version"]


@pytest.mark.asyncio
async def test_version_endpoint_server_version_from_env(
    client: AsyncClient, monkeypatch
):
    monkeypatch.setenv("STARDAG_SERVER_VERSION", "1.2.3")
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json()["server_version"] == "1.2.3"


@pytest.fixture
def ui_dist(tmp_path: Path):
    """A minimal built-UI directory; unmounts the UI from the shared app after."""
    (tmp_path / "index.html").write_text("<html><body>stardag ui</body></html>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "main.js").write_text("console.log('ui')")
    yield tmp_path
    # create_app mounts onto the shared app from stardag_api.main; remove the
    # mount so other tests see the API-only app.
    from stardag_api.main import app as api_app

    api_app.router.routes[:] = [
        route
        for route in api_app.router.routes
        if getattr(route, "name", None) != UI_MOUNT_NAME
    ]


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_app_without_ui_dist_is_api_only():
    app = create_app(None)
    assert not any(
        getattr(route, "name", None) == UI_MOUNT_NAME for route in app.router.routes
    )
    async with _client(app) as client:
        response = await client.get("/some/spa/route")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_app_with_missing_dir_is_api_only(tmp_path: Path):
    app = create_app(str(tmp_path / "does-not-exist"))
    assert not any(
        getattr(route, "name", None) == UI_MOUNT_NAME for route in app.router.routes
    )


@pytest.mark.asyncio
async def test_create_app_serves_ui_with_spa_fallback(ui_dist: Path):
    app = create_app(str(ui_dist))
    async with _client(app) as client:
        # Index
        response = await client.get("/")
        assert response.status_code == 200
        assert "stardag ui" in response.text
        # Static asset
        response = await client.get("/assets/main.js")
        assert response.status_code == 200
        # SPA fallback: unknown path serves index.html
        response = await client.get("/builds/some-client-side-route")
        assert response.status_code == 200
        assert "stardag ui" in response.text
        # API routes still win over the mount
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
        response = await client.get("/api/v1/version")
        assert response.status_code == 200
        response = await client.get("/.well-known/jwks.json")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_mount_ui_is_idempotent(ui_dist: Path):
    app = create_app(str(ui_dist))
    mount_ui(app, ui_dist)  # second mount must be a no-op
    ui_mounts = [
        route
        for route in app.router.routes
        if getattr(route, "name", None) == UI_MOUNT_NAME
    ]
    assert len(ui_mounts) == 1
