"""End-to-end tests for the gzip request-body middleware.

The SDK gzips request bodies above ~1KB on bulk-register paths;
``GZipRequestMiddleware`` decompresses them transparently before route
handlers see the body. These tests verify the round-trip via the test
client, plus the pass-through and error paths.
"""

from __future__ import annotations

import gzip
import json

import pytest
from httpx import AsyncClient


def _gzip_json(body: dict) -> bytes:
    return gzip.compress(json.dumps(body, separators=(",", ":")).encode())


@pytest.mark.asyncio
async def test_gzipped_bulk_register_round_trips(client: AsyncClient):
    """A gzipped bulk-register POST is decompressed server-side and
    handled identically to a non-gzipped POST: same response body, same
    Task rows persisted."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    payload = {
        "tasks": [
            {
                "task_id": f"gzip-task-{i}",
                "task_namespace": "",
                "task_name": "GzipTask",
                "task_data": {"i": i},
            }
            for i in range(5)
        ]
    }

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        content=_gzip_json(payload),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert response.status_code == 201
    returned_ids = [t["task_id"] for t in response.json()["tasks"]]
    assert returned_ids == [f"gzip-task-{i}" for i in range(5)]

    # The list endpoint sees the rows that the gzip request created.
    listed = (await client.get(f"/api/v1/builds/{build_id}/tasks")).json()
    assert {t["task_id"] for t in listed} == set(returned_ids)


@pytest.mark.asyncio
async def test_non_gzipped_request_is_pass_through(client: AsyncClient):
    """Requests without ``Content-Encoding: gzip`` go through the
    middleware untouched. This is the path old SDKs take, and also the
    path direct ``curl`` users take."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "no-encoding-task",
            "task_namespace": "",
            "task_name": "Plain",
            "task_data": {},
        },
    )
    assert response.status_code == 201
    assert response.json()["task_id"] == "no-encoding-task"


@pytest.mark.asyncio
async def test_unknown_content_encoding_passes_through(client: AsyncClient):
    """Only ``Content-Encoding: gzip`` triggers decompression. An
    unknown encoding header is treated as no encoding (the route handler
    receives the body as-is, which would normally be a JSON parse — same
    pre-existing behaviour as before the middleware was added)."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    body = {
        "task_id": "br-task",
        "task_namespace": "",
        "task_name": "BrTask",
        "task_data": {},
    }
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        content=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "br",  # Brotli, not supported.
        },
    )
    # Body was plain JSON, so the handler should still parse it
    # successfully — the middleware's only job is to reject *gzipped*
    # bodies it can't handle, not to second-guess unknown encodings.
    assert response.status_code == 201
    assert response.json()["task_id"] == "br-task"


@pytest.mark.asyncio
async def test_malformed_gzip_returns_400(client: AsyncClient):
    """Body claims gzip but isn't valid gzip data → 400 with a clear
    detail. Important so a buggy client doesn't get a confusing
    downstream "JSON parse failed" or 500."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/bulk",
        content=b"this is not gzip data",
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert response.status_code == 400
    assert "gzip" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_gzip_bomb_aborts_with_413(client: AsyncClient, monkeypatch):
    """Streaming decompression must abort the moment the decompressed
    output crosses the cap — without first allocating the full output.
    A small compressed body that inflates to >cap returns 413."""
    from stardag_api.middleware import gzip_request as gzip_mw

    # Lower the cap so the test stays fast and obvious.
    monkeypatch.setattr(gzip_mw, "_MAX_DECOMPRESSED_BYTES", 1024)

    # 100 KB of zeros compresses to ~100 bytes — small compressed,
    # 100× the configured decompressed cap.
    bomb = gzip.compress(b"\x00" * (100 * 1024))
    assert len(bomb) < 1024, "bomb should be small compressed"

    response = await client.post(
        "/api/v1/builds",
        content=bomb,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert response.status_code == 413
    assert "decompressed" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_oversized_compressed_body_rejected(client: AsyncClient, monkeypatch):
    """If the compressed input itself exceeds the cap we reject before
    spending CPU on decompression. First line of defence."""
    from stardag_api.middleware import gzip_request as gzip_mw

    monkeypatch.setattr(gzip_mw, "_MAX_COMPRESSED_BYTES", 256)

    # gzip.compress on random-ish bytes won't compress well; we just
    # need the *compressed* output to exceed 256 B. Use repeated random
    # garbage that gzip can't deflate efficiently.
    import os

    body = os.urandom(2048)  # ~2 KB; gzip overhead keeps it >256 B.
    payload = gzip.compress(body)
    assert len(payload) > 256

    response = await client.post(
        "/api/v1/builds",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert response.status_code == 413
    assert "compressed" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_gzipped_single_task_register(client: AsyncClient):
    """Even single-task POST works under gzip — the middleware doesn't
    care which route the request is for, just whether the body is
    gzipped. Demonstrates the middleware isn't bulk-endpoint-specific."""
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    body = {
        "task_id": "gzipped-single",
        "task_namespace": "",
        "task_name": "Single",
        "task_data": {"some": "data"},
    }
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        content=_gzip_json(body),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert response.status_code == 201
    assert response.json()["task_id"] == "gzipped-single"
