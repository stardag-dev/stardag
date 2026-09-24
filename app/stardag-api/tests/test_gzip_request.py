"""Tests for the gzip request-body middleware.

The SDK gzips request bodies above ~1KB on bulk-register paths;
``GZipRequestMiddleware`` decompresses them transparently before route
handlers see the body. The middleware is route-agnostic, so it is tested
on a minimal echo app rather than on a registration route: what it
promises is that a handler sees the same bytes with or without
``Content-Encoding: gzip``, and that bad or oversized gzip is refused
before any handler runs.
"""

from __future__ import annotations

import gzip
import json
import os

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from stardag_api.middleware import GZipRequestMiddleware


def _gzip_json(body: dict) -> bytes:
    return gzip.compress(json.dumps(body, separators=(",", ":")).encode())


@pytest.fixture
async def echo_client():
    """A client for an app whose one route echoes the JSON body it parsed."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"received": await request.json()}

    app.add_middleware(GZipRequestMiddleware)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_gzipped_body_round_trips(echo_client: AsyncClient):
    """A gzipped POST reaches the handler as the JSON it was compressed from."""
    payload = {"tasks": [{"task_id": f"gzip-task-{i}", "i": i} for i in range(5)]}
    response = await echo_client.post(
        "/echo",
        content=_gzip_json(payload),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.json()["received"] == payload


async def test_non_gzipped_request_is_pass_through(echo_client: AsyncClient):
    """Requests without ``Content-Encoding: gzip`` go through untouched —
    the path direct ``curl`` users take."""
    response = await echo_client.post("/echo", json={"task_id": "plain"})
    assert response.status_code == 200
    assert response.json()["received"] == {"task_id": "plain"}


async def test_unknown_content_encoding_passes_through(echo_client: AsyncClient):
    """Only ``Content-Encoding: gzip`` triggers decompression: an unknown
    encoding header is treated as no encoding, so a plain JSON body with a
    ``br`` header still parses. The middleware refuses only *gzipped*
    bodies it cannot handle; it does not second-guess other encodings."""
    body = {"task_id": "br-task"}
    response = await echo_client.post(
        "/echo",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Content-Encoding": "br"},
    )
    assert response.status_code == 200
    assert response.json()["received"] == body


async def test_malformed_gzip_returns_400(echo_client: AsyncClient):
    """Body claims gzip but isn't valid gzip data → 400 with a clear detail,
    not a confusing downstream "JSON parse failed" or 500."""
    response = await echo_client.post(
        "/echo",
        content=b"this is not gzip data",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 400
    assert "gzip" in response.json()["detail"].lower()


async def test_gzip_bomb_aborts_with_413(echo_client: AsyncClient, monkeypatch):
    """Streaming decompression aborts the moment the decompressed output
    crosses the cap, without first allocating the full output."""
    from stardag_api.middleware import gzip_request as gzip_mw

    monkeypatch.setattr(gzip_mw, "_MAX_DECOMPRESSED_BYTES", 1024)
    # 100 KB of zeros compresses to ~100 bytes: 100× the configured cap.
    bomb = gzip.compress(b"\x00" * (100 * 1024))
    assert len(bomb) < 1024, "bomb should be small compressed"

    response = await echo_client.post(
        "/echo",
        content=bomb,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 413
    assert "decompressed" in response.json()["detail"].lower()


async def test_oversized_compressed_body_rejected(
    echo_client: AsyncClient, monkeypatch
):
    """A compressed input over the cap is refused before any decompression."""
    from stardag_api.middleware import gzip_request as gzip_mw

    monkeypatch.setattr(gzip_mw, "_MAX_COMPRESSED_BYTES", 256)
    # Random bytes don't deflate, so the compressed payload stays > 256 B.
    payload = gzip.compress(os.urandom(2048))
    assert len(payload) > 256

    response = await echo_client.post(
        "/echo",
        content=payload,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 413
    assert "compressed" in response.json()["detail"].lower()
