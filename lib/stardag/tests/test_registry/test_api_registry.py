"""Unit tests for APIRegistry helpers."""

from __future__ import annotations

import gzip
import json

import pytest

from stardag.exceptions import APIError, NotFoundError
from stardag.registry._api_registry import (
    _GZIP_REQUEST_THRESHOLD_BYTES,
    _is_route_not_found,
    _maybe_gzip_json_body,
)


class TestMaybeGzipJsonBody:
    """Decision boundary for the gzip-on-request behaviour."""

    def test_none_body_returns_no_content(self):
        content, headers = _maybe_gzip_json_body(None)
        assert content is None
        assert headers == {}

    def test_small_body_is_not_gzipped(self):
        body = {"task_id": "x", "task_name": "y"}
        content, headers = _maybe_gzip_json_body(body)
        assert content is not None
        assert len(content) < _GZIP_REQUEST_THRESHOLD_BYTES
        assert headers == {"Content-Type": "application/json"}
        # Round-trips as JSON without decompression.
        assert json.loads(content) == body

    def test_large_body_is_gzipped(self):
        # Build a body well above the threshold with repeated structure
        # (representative of bulk-register payloads).
        body = {
            "tasks": [
                {
                    "task_id": f"task-{i:08d}",
                    "task_namespace": "demo.namespace",
                    "task_name": "RepeatedTask",
                    "task_data": {"index": i, "label": "x" * 32},
                    "dependency_task_ids": [],
                }
                for i in range(50)
            ]
        }
        content, headers = _maybe_gzip_json_body(body)
        assert content is not None
        assert headers["Content-Type"] == "application/json"
        assert headers["Content-Encoding"] == "gzip"
        # Decompresses back to the original JSON.
        assert json.loads(gzip.decompress(content)) == body
        # And actually saved bytes — repeated keys/structure should
        # compress well below the original.
        original_bytes = json.dumps(body, separators=(",", ":")).encode()
        assert len(content) < len(original_bytes) // 2, (
            f"Expected at least 2x compression on a structured bulk "
            f"payload; got {len(original_bytes)} -> {len(content)}"
        )

    def test_threshold_exact_boundary(self):
        # Construct a payload whose serialised size is exactly at the
        # threshold to verify the strict-less-than boundary
        # (``< threshold`` => raw, ``>= threshold`` => gzipped).
        # The ``key`` value is sized so the final dump hits the boundary.
        target = _GZIP_REQUEST_THRESHOLD_BYTES
        # ``{"k":"<padding>"}``: braces+quotes+colon+commas = 8 bytes
        # of overhead (no commas here, just `{"k":""}` = 8 bytes).
        padding_len = target - len('{"k":""}')
        body = {"k": "x" * padding_len}
        encoded = json.dumps(body, separators=(",", ":")).encode()
        assert len(encoded) == target, "size-control assumption broke"
        content, headers = _maybe_gzip_json_body(body)
        # At the threshold we DO compress.
        assert headers.get("Content-Encoding") == "gzip"
        assert content is not None and gzip.decompress(content) == encoded


class TestAPIRegistryGzipsWireFormat:
    """End-to-end check that ``APIRegistry`` *actually* emits gzipped
    bytes on the wire for bulk-register payloads — not just that the
    helper function would, if invoked. Uses ``httpx.MockTransport`` to
    capture the outgoing request before it hits the network.
    """

    def _make_registry_and_capture(self):
        """Build an APIRegistry whose sync httpx client points at a
        MockTransport that records every outgoing request. Returns
        (registry, captured_requests_list)."""
        import httpx

        from stardag.registry._api_registry import APIRegistry

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            # Realistic bulk-register response shape (one TaskResponse per
            # task in payload), enough for APIRegistry to consider the
            # call successful.
            decompressed_body = (
                gzip.decompress(request.content)
                if request.headers.get("content-encoding") == "gzip"
                else request.content
            )
            payload = json.loads(decompressed_body)
            tasks = payload.get("tasks", [])
            from uuid import uuid4

            return httpx.Response(
                201,
                json={
                    "tasks": [
                        {
                            "id": str(uuid4()),
                            "task_id": t["task_id"],
                            "environment_id": "00000000-0000-0000-0000-000000000003",
                            "task_namespace": t.get("task_namespace", ""),
                            "task_name": t["task_name"],
                            "task_data": t["task_data"],
                            "version": None,
                            "output_uri": None,
                            "created_at": "2026-04-30T00:00:00+00:00",
                            "is_phantom": False,
                        }
                        for t in tasks
                    ]
                },
            )

        # Instantiate APIRegistry with explicit creds so it doesn't
        # depend on environment variables. Override the inner httpx
        # client with our MockTransport-backed one.
        registry = APIRegistry(
            api_url="http://test.invalid",
            api_key="test-key",
        )
        registry._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            auth=registry._auth,
        )
        return registry, captured

    def _make_fake_task(self, task_id: str, task_data: dict):
        """Produce an object that ``_get_task_data_for_registration``
        accepts. Avoids spinning up a real Task subclass — we only need
        the fields the helper reads. ``id`` is set per-instance so a
        batch of fake tasks has 50 distinct UUIDs (otherwise a class-
        attribute UUID would make the bulk gzip test silently exercise
        50 *identical* tasks, which is not the realistic payload we
        want to compress)."""
        from uuid import uuid4

        class _Fake:
            version = ""

            def __init__(self, tid, td):
                self.id = uuid4()
                self._tid = tid
                self._td = td

            def get_namespace(self):
                return "test"

            def get_name(self):
                return "FakeTask"

            def model_dump(self, mode="json"):
                return self._td

            def requires(self):
                return ()

        return _Fake(task_id, task_data)

    def test_large_bulk_register_uses_gzip_on_wire(self):
        registry, captured = self._make_registry_and_capture()

        # 50 tasks × ~150 bytes serialised each ≈ 7.5KB > 1KB threshold.
        tasks = [
            self._make_fake_task(
                f"big-task-{i:04d}",
                {"index": i, "label": "x" * 64, "extra": "padding-" * 8},
            )
            for i in range(50)
        ]
        registry.task_register_bulk(
            build_id=__import__("uuid").UUID("00000000-0000-0000-0000-000000000001"),
            tasks=tasks,  # type: ignore[arg-type]
        )

        assert len(captured) == 1
        request = captured[0]
        assert request.headers.get("content-encoding") == "gzip", (
            f"Expected Content-Encoding: gzip on big bulk request; "
            f"headers were {dict(request.headers)}"
        )
        # SDK opts into the slim ``id_only=true`` response shape since
        # it doesn't read the response body — this saves ~10× on
        # response size at the server.
        assert request.url.params.get("id_only") == "true", (
            f"Expected id_only=true on bulk register request; "
            f"params were {dict(request.url.params)}"
        )
        # Decompressed body round-trips back to JSON with all 50 tasks,
        # each carrying a distinct task_id (UUID per-instance — proves
        # we're really compressing a batch of unique tasks, not 50
        # copies of one).
        decoded = json.loads(gzip.decompress(request.content))
        assert len(decoded["tasks"]) == 50
        sent_ids = {t["task_id"] for t in decoded["tasks"]}
        assert len(sent_ids) == 50

    def test_small_single_register_does_not_gzip(self):
        registry, captured = self._make_registry_and_capture()

        task = self._make_fake_task("small-task", {"x": 1})
        registry.task_register(
            build_id=__import__("uuid").UUID("00000000-0000-0000-0000-000000000001"),
            task=task,  # type: ignore[arg-type]
        )

        assert len(captured) == 1
        request = captured[0]
        # Tiny payload — gzip overhead would be a net loss, so no
        # Content-Encoding header.
        assert "content-encoding" not in {k.lower() for k in request.headers.keys()}, (
            f"Did not expect gzip on a single small task; headers were "
            f"{dict(request.headers)}"
        )
        # Body is plain JSON (not gzip-prefixed bytes).
        decoded = json.loads(request.content)
        assert "task_id" in decoded
        assert decoded["task_name"] == "FakeTask"


class TestIsRouteNotFound:
    """Narrow-404 detection used by task_add_dependencies for backward compat."""

    def test_fastapi_default_is_route_not_found(self):
        """FastAPI's default unknown-path response: detail == 'Not Found'."""
        err = NotFoundError("op: resource not found", detail="Not Found")
        assert _is_route_not_found(err) is True

    def test_build_not_found_is_not_route(self):
        """An app-level 'Build not found' must NOT be treated as route-missing."""
        err = NotFoundError("op: resource not found", detail="Build not found")
        assert _is_route_not_found(err) is False

    def test_task_not_registered_is_not_route(self):
        err = NotFoundError(
            "op: resource not found",
            detail="Task abc not registered in this environment",
        )
        assert _is_route_not_found(err) is False

    def test_none_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail=None)
        assert _is_route_not_found(err) is False

    def test_empty_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail="")
        assert _is_route_not_found(err) is False

    def test_structured_detail_is_not_route(self):
        # When detail is a dict stringified by _handle_response_error
        err = NotFoundError(
            "op: resource not found", detail="{'error_code': 'X', 'message': 'Y'}"
        )
        assert _is_route_not_found(err) is False

    def test_accepts_notfounderror_only(self):
        # Signature sanity: helper takes NotFoundError; APIError with a 404 status
        # is unusual but we don't check status_code, only detail.
        err = APIError("misc", status_code=500, detail="Not Found")
        # The helper reads .detail directly, so a non-NotFoundError would still
        # return True if detail matches — not our concern; call sites only pass
        # NotFoundError. This test documents the contract.
        assert _is_route_not_found(err) is True  # type: ignore[arg-type]


class TestTaskSkip404Swallow:
    """``task_skip`` / ``task_skip_aio`` swallow FastAPI's ``Not Found`` 404
    with a warning so a new SDK against an old API (no ``/skip`` route)
    does not fail builds on every fail-fast / blocked-dep path. Genuine
    app-level 404s (e.g. unknown build_id) must still propagate.
    """

    def _make_registry_and_handler(self, response_factory):
        """Build an APIRegistry with the sync httpx client routed to handler.

        For async tests the caller must additionally inject the mock
        AsyncClient *inside the test coroutine* (so the captured loop
        matches the lazy-init check in ``_get_async_client``):

            import asyncio, httpx
            registry._async_client = httpx.AsyncClient(
                transport=httpx.MockTransport(response_factory),
                auth=registry._auth,
            )
            registry._async_client_loop = asyncio.get_running_loop()
        """
        import httpx

        from stardag.registry._api_registry import APIRegistry

        registry = APIRegistry(api_url="http://test.invalid", api_key="test-key")
        registry._client = httpx.Client(
            transport=httpx.MockTransport(response_factory),
            auth=registry._auth,
        )
        return registry

    @staticmethod
    def _inject_async_mock(registry, response_factory):
        import asyncio
        import httpx

        registry._async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(response_factory),
            auth=registry._auth,
        )
        registry._async_client_loop = asyncio.get_running_loop()

    def _fake_task(self):
        from uuid import uuid4

        class _Fake:
            id = uuid4()

            def get_namespace(self):
                return "test"

            def get_name(self):
                return "FakeTask"

        return _Fake()

    def test_sync_route_missing_404_is_swallowed(self, caplog):
        """Old API: detail == 'Not Found' → warn + return, no exception."""
        import httpx
        import logging
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not Found"})

        registry = self._make_registry_and_handler(handler)
        task = self._fake_task()

        with caplog.at_level(logging.WARNING):
            # Should NOT raise.
            registry.task_skip(
                build_id=UUID("00000000-0000-0000-0000-000000000001"),
                task=task,  # type: ignore[arg-type]
            )

        assert any(
            "does not support POST /skip" in rec.message for rec in caplog.records
        ), f"Expected route-missing warning; got: {[r.message for r in caplog.records]}"

    def test_sync_app_level_404_propagates(self):
        """New API: detail == 'Build not found' → raise NotFoundError as usual."""
        import httpx
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Build not found"})

        registry = self._make_registry_and_handler(handler)
        task = self._fake_task()

        with pytest.raises(NotFoundError):
            registry.task_skip(
                build_id=UUID("00000000-0000-0000-0000-000000000001"),
                task=task,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_aio_route_missing_404_is_swallowed(self, caplog):
        """Async path: detail == 'Not Found' → warn + return."""
        import httpx
        import logging
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not Found"})

        registry = self._make_registry_and_handler(handler)
        self._inject_async_mock(registry, handler)
        task = self._fake_task()

        with caplog.at_level(logging.WARNING):
            await registry.task_skip_aio(
                build_id=UUID("00000000-0000-0000-0000-000000000001"),
                task=task,  # type: ignore[arg-type]
            )

        assert any(
            "does not support POST /skip" in rec.message for rec in caplog.records
        ), f"Expected route-missing warning; got: {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_aio_app_level_404_propagates(self):
        """Async path: app-level 404 propagates."""
        import httpx
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Build not found"})

        registry = self._make_registry_and_handler(handler)
        self._inject_async_mock(registry, handler)
        task = self._fake_task()

        with pytest.raises(NotFoundError):
            await registry.task_skip_aio(
                build_id=UUID("00000000-0000-0000-0000-000000000001"),
                task=task,  # type: ignore[arg-type]
            )
