"""Unit tests for APIRegistry helpers."""

from __future__ import annotations

import gzip
import json
from uuid import UUID

import pytest

from stardag.exceptions import APIError, NotFoundError, is_missing_route_error
from stardag.registry._api_registry import (
    _GZIP_REQUEST_THRESHOLD_BYTES,
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

    def test_fastapi_defaultis_missing_route_error(self):
        """FastAPI's default unknown-path response: detail == 'Not Found'."""
        err = NotFoundError("op: resource not found", detail="Not Found")
        assert is_missing_route_error(err) is True

    def test_build_not_found_is_not_route(self):
        """An app-level 'Build not found' must NOT be treated as route-missing."""
        err = NotFoundError("op: resource not found", detail="Build not found")
        assert is_missing_route_error(err) is False

    def test_task_not_registered_is_not_route(self):
        err = NotFoundError(
            "op: resource not found",
            detail="Task abc not registered in this environment",
        )
        assert is_missing_route_error(err) is False

    def test_none_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail=None)
        assert is_missing_route_error(err) is False

    def test_empty_detail_is_not_route(self):
        err = NotFoundError("op: resource not found", detail="")
        assert is_missing_route_error(err) is False

    def test_structured_detail_is_not_route(self):
        # When detail is a dict stringified by _handle_response_error
        err = NotFoundError(
            "op: resource not found", detail="{'error_code': 'X', 'message': 'Y'}"
        )
        assert is_missing_route_error(err) is False

    def test_accepts_notfounderror_only(self):
        # Signature sanity: helper takes NotFoundError; APIError with a 404 status
        # is unusual but we don't check status_code, only detail.
        err = APIError("misc", status_code=500, detail="Not Found")
        # The helper reads .detail directly, so a non-NotFoundError would still
        # return True if detail matches — not our concern; call sites only pass
        # NotFoundError. This test documents the contract.
        assert is_missing_route_error(err) is True  # type: ignore[arg-type]


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


class TestBuildResume404Swallow:
    """``build_resume`` / ``build_resume_aio`` follow the same backward-
    compat 404 pattern as ``task_skip``: an old API that does not yet
    expose the ``/builds/{id}/resume`` route returns FastAPI's default
    ``Not Found`` body, which the SDK swallows with a warning so resumed
    builds keep working (just without the registry-side status flip).
    Genuine app-level 404s (e.g. unknown build_id) still propagate.
    """

    def _make_registry_and_handler(self, response_factory):
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

    def test_sync_route_missing_404_is_swallowed(self, caplog):
        """Old API: detail == 'Not Found' → warn + return, no exception."""
        import httpx
        import logging
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not Found"})

        registry = self._make_registry_and_handler(handler)

        with caplog.at_level(logging.WARNING):
            registry.build_resume(UUID("00000000-0000-0000-0000-000000000001"))

        assert any(
            "does not support POST" in rec.message and "/resume" in rec.message
            for rec in caplog.records
        ), f"Expected route-missing warning; got: {[r.message for r in caplog.records]}"

    def test_sync_app_level_404_propagates(self):
        """New API: detail == 'Build not found' → raise NotFoundError."""
        import httpx
        from uuid import UUID

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Build not found"})

        registry = self._make_registry_and_handler(handler)

        with pytest.raises(NotFoundError):
            registry.build_resume(UUID("00000000-0000-0000-0000-000000000001"))

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

        with caplog.at_level(logging.WARNING):
            await registry.build_resume_aio(
                UUID("00000000-0000-0000-0000-000000000001")
            )

        assert any(
            "does not support POST" in rec.message and "/resume" in rec.message
            for rec in caplog.records
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

        with pytest.raises(NotFoundError):
            await registry.build_resume_aio(
                UUID("00000000-0000-0000-0000-000000000001")
            )


class TestConcurrencyLimitPathEncoding:
    """Concurrency-limit keys / task ids are URL-encoded per path segment.

    A key created from the UI may contain ``/`` or other reserved
    characters; interpolating it raw into the URL path would break routing
    (a ``/`` splits into extra path segments) or hit the wrong endpoint.
    Each embedded segment must be percent-encoded with ``safe=""`` so even
    ``/`` is escaped.
    """

    def _make_registry_and_capture(self):
        import httpx

        from stardag.registry._api_registry import APIRegistry

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True, "holders": [], "total": 0})

        registry = APIRegistry(api_url="http://test.invalid", api_key="test-key")
        registry._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            auth=registry._auth,
        )
        return registry, captured

    @staticmethod
    def _encoded_path(request) -> str:
        # ``url.path`` percent-decodes for display; ``raw_path`` preserves the
        # bytes actually put on the wire (and appends the query string, which
        # we strip here). This is what proves the segment was encoded.
        return request.url.raw_path.decode().split("?", 1)[0]

    def test_set_encodes_key_segment(self):
        registry, captured = self._make_registry_and_capture()
        registry.concurrency_limit_set("a/b c#d", 3)
        assert len(captured) == 1
        # The raw key never appears verbatim; ``/`` and other reserved chars
        # are percent-encoded so the whole key stays a single path segment.
        assert (
            self._encoded_path(captured[0])
            == "/api/v1/concurrency-limits/a%2Fb%20c%23d"
        )

    def test_delete_encodes_key_segment(self):
        registry, captured = self._make_registry_and_capture()
        registry.concurrency_limit_delete("a/b")
        assert self._encoded_path(captured[0]) == "/api/v1/concurrency-limits/a%2Fb"

    def test_holders_encodes_key_segment(self):
        registry, captured = self._make_registry_and_capture()
        registry.concurrency_limit_holders("a/b")
        assert (
            self._encoded_path(captured[0])
            == "/api/v1/concurrency-limits/a%2Fb/holders"
        )

    def test_evict_encodes_both_segments(self):
        registry, captured = self._make_registry_and_capture()
        registry.concurrency_limit_evict("a/b", "id/1")
        assert (
            self._encoded_path(captured[0])
            == "/api/v1/concurrency-limits/a%2Fb/holders/id%2F1/evict"
        )


class _CapturingRegistry:
    """An APIRegistry whose sync client is a MockTransport recorder.

    Shared by the operator-surface tests below: they are all about what
    goes on the wire (query params, request body, path) and what comes
    back off it (model parsing), so a canned response plus the captured
    request is the whole fixture.
    """

    def __init__(self, response_json: dict, status_code: int = 200):
        import httpx

        from stardag.registry._api_registry import APIRegistry

        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status_code, json=response_json)

        self.registry = APIRegistry(api_url="http://test.invalid", api_key="test-key")
        self.registry._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            auth=self.registry._auth,
        )

    @property
    def request(self):
        assert len(self.requests) == 1, f"expected 1 request, got {len(self.requests)}"
        return self.requests[0]


_BUILD_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_BUILD_ID = "22222222-2222-2222-2222-222222222222"


class TestBuildList:
    """``GET /builds`` — filters go server-side, unknown fields are ignored."""

    def _page(self, **build_overrides):
        build = {
            "id": _BUILD_ID,
            "name": "spring-otter-42",
            "status": "running",
            "last_active_at": "2026-07-01T10:00:00+00:00",
            "last_activity_at": "2026-07-04T11:30:00+00:00",
            # A field this SDK version does not model: forward compatibility
            # requires it to be ignored, not to raise.
            "some_future_field": {"nested": True},
        }
        build.update(build_overrides)
        return {"builds": [build], "total": 7, "page": 2, "page_size": 5}

    def test_filters_ride_as_query_params(self):
        cap = _CapturingRegistry(self._page())
        result = cap.registry.build_list(
            page=2,
            page_size=5,
            status="running",
            reactive_app_name="my-app",
            idle_for_seconds=86400,
        )
        params = cap.request.url.params
        assert params["page"] == "2"
        assert params["page_size"] == "5"
        assert params["status"] == "running"
        assert params["reactive_app_name"] == "my-app"
        assert params["idle_for_seconds"] == "86400"
        assert result.total == 7
        assert result.page == 2
        assert result.builds[0].name == "spring-otter-42"

    def test_unset_filters_are_omitted(self):
        cap = _CapturingRegistry(self._page())
        cap.registry.build_list()
        params = cap.request.url.params
        assert "status" not in params
        assert "reactive_app_name" not in params
        assert "idle_for_seconds" not in params

    def test_unknown_response_fields_are_ignored(self):
        cap = _CapturingRegistry(self._page())
        result = cap.registry.build_list()
        build = result.builds[0]
        assert not hasattr(build, "some_future_field")
        # Both liveness timestamps are parsed and kept distinct.
        assert build.last_active_at is not None
        assert build.last_activity_at is not None
        assert build.last_active_at != build.last_activity_at

    def test_missing_liveness_fields_default_to_none(self):
        """An older server sends neither timestamp; parsing must not fail."""
        cap = _CapturingRegistry(
            {
                "builds": [{"id": _BUILD_ID, "name": "old-server-build"}],
                "total": 1,
                "page": 1,
                "page_size": 20,
            }
        )
        build = cap.registry.build_list().builds[0]
        assert build.last_activity_at is None
        assert build.status is None


class TestBuildListRunning:
    """The watchdog sweep must filter server-side, not client-side."""

    def test_passes_status_running_to_the_server(self):
        cap = _CapturingRegistry(
            {
                "builds": [{"id": _BUILD_ID, "name": "b1", "status": "running"}],
                "total": 1,
                "page": 1,
                "page_size": 100,
            }
        )
        running = cap.registry.build_list_running(limit=10)
        assert running == [UUID(_BUILD_ID)]
        # Server-side filter: the sweep must not page an unfiltered list and
        # match in Python, or an environment full of non-running builds can
        # starve it of the running ones it exists to find.
        assert cap.request.url.params["status"] == "running"

    def test_stops_paging_on_a_short_page(self):
        cap = _CapturingRegistry(
            {
                "builds": [{"id": _BUILD_ID, "name": "b1", "status": "running"}],
                "total": 1,
                "page": 1,
                "page_size": 100,
            }
        )
        cap.registry.build_list_running(limit=100)
        assert len(cap.requests) == 1


class TestBuildCancelCascade:
    def test_cascade_param_and_parsed_result(self):
        cap = _CapturingRegistry(
            {
                "id": _BUILD_ID,
                "name": "spring-otter-42",
                "cascaded_task_ids": ["task-a", "task-b"],
                "cascaded_task_count": 2,
            }
        )
        result = cap.registry.build_cancel(UUID(_BUILD_ID), cascade=True)
        assert cap.request.url.params["cascade"] == "true"
        assert result is not None
        assert result.cascaded_task_ids == ["task-a", "task-b"]

    def test_cascade_omitted_by_default(self):
        cap = _CapturingRegistry({"id": _BUILD_ID, "name": "b"})
        result = cap.registry.build_cancel(UUID(_BUILD_ID))
        assert "cascade" not in cap.request.url.params
        # A server predating the cascade returns no cascade fields; they
        # default, which reads as "nothing was cascaded".
        assert result is not None
        assert result.cascaded_task_ids == []
        assert result.cascaded_task_count == 0


class TestBuildBulkCancel:
    def _response(self, **overrides):
        payload = {
            "dry_run": True,
            "builds": [
                {
                    "build_id": _BUILD_ID,
                    "name": "spring-otter-42",
                    "last_activity_at": "2026-07-01T10:00:00+00:00",
                    "cascaded_task_ids": ["task-a"],
                }
            ],
            "build_count": 1,
            "task_count": 1,
            "skipped": {_OTHER_BUILD_ID: "not_running"},
            "truncated": True,
        }
        payload.update(overrides)
        return payload

    def test_body_carries_only_the_filters_that_were_set(self):
        cap = _CapturingRegistry(self._response())
        cap.registry.build_bulk_cancel(idle_for_seconds=86400, dry_run=True)
        body = json.loads(cap.request.content)
        assert body["idle_for_seconds"] == 86400
        assert body["dry_run"] is True
        # Defaults the caller did not touch are still sent explicitly (the
        # SDK's defaults and the server's must not silently diverge), but
        # unset optional filters are absent so the server decides.
        assert body["cascade"] is True
        assert "build_ids" not in body
        assert "reactive_app_name" not in body
        assert "reason" not in body

    def test_build_ids_are_stringified(self):
        cap = _CapturingRegistry(self._response())
        cap.registry.build_bulk_cancel(build_ids=[UUID(_BUILD_ID), _OTHER_BUILD_ID])
        body = json.loads(cap.request.content)
        assert body["build_ids"] == [_BUILD_ID, _OTHER_BUILD_ID]

    def test_result_is_parsed_including_skipped_and_truncated(self):
        cap = _CapturingRegistry(self._response())
        result = cap.registry.build_bulk_cancel(idle_for_seconds=60)
        assert result.dry_run is True
        assert result.build_count == 1
        assert result.task_count == 1
        assert result.skipped == {_OTHER_BUILD_ID: "not_running"}
        assert result.truncated is True
        assert result.builds[0].cascaded_task_ids == ["task-a"]


class TestTaskList:
    def _response(self, **overrides):
        payload = {
            "tasks": [
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "task_id": "task-abc",
                    "task_namespace": "demo.pipeline",
                    "task_name": "TrainModel",
                    "latest_status": "running",
                    "latest_status_at": "2026-07-04T09:00:00+00:00",
                    "latest_status_build_id": _OTHER_BUILD_ID,
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 20,
        }
        payload.update(overrides)
        return payload

    def test_status_is_a_repeated_query_param(self):
        """A dict cannot express two values for one key; a pair list can."""
        cap = _CapturingRegistry(self._response())
        cap.registry.task_list(status=["running", "suspended"])
        assert cap.request.url.params.get_list("status") == ["running", "suspended"]

    def test_status_older_than_is_sent_as_iso8601(self):
        from datetime import datetime, timezone

        cap = _CapturingRegistry(self._response())
        cutoff = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)
        cap.registry.task_list(status_older_than=cutoff)
        assert (
            cap.request.url.params["status_older_than"] == "2026-07-04T09:00:00+00:00"
        )

    def test_claim_holder_fields_are_parsed(self):
        cap = _CapturingRegistry(self._response())
        task = cap.registry.task_list(status=["running"]).tasks[0]
        assert task.latest_status == "running"
        assert task.latest_status_at is not None
        assert str(task.latest_status_build_id) == _OTHER_BUILD_ID

    def test_unset_filters_are_omitted(self):
        cap = _CapturingRegistry(self._response())
        cap.registry.task_list()
        params = cap.request.url.params
        assert "status" not in params
        assert "status_older_than" not in params
        assert "task_name" not in params


class TestTaskByIdEndpoints:
    """Cancel/retry addressed by id hit the same routes as the task-object variants."""

    def test_cancel_by_id_path(self):
        cap = _CapturingRegistry({})
        cap.registry.task_cancel_by_id(UUID(_BUILD_ID), "task-abc")
        assert (
            cap.request.url.path == f"/api/v1/builds/{_BUILD_ID}/tasks/task-abc/cancel"
        )

    def test_retry_by_id_path(self):
        cap = _CapturingRegistry({})
        cap.registry.task_retry_by_id(UUID(_BUILD_ID), "task-abc")
        assert (
            cap.request.url.path == f"/api/v1/builds/{_BUILD_ID}/tasks/task-abc/retry"
        )


class TestTickSummaries:
    def test_report_posts_the_summary_verbatim(self):
        cap = _CapturingRegistry(
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "build_id": _BUILD_ID,
                "outcome": "terminal",
                "summary": {"outcome": "terminal"},
                "created_at": "2026-07-04T09:00:00+00:00",
            },
            status_code=201,
        )
        summary = {"outcome": "terminal", "spawned": 3, "some_future_counter": 1}
        cap.registry.build_report_tick_summary(UUID(_BUILD_ID), summary)
        assert cap.request.url.path == f"/api/v1/builds/{_BUILD_ID}/tick-summaries"
        # The body is the summary as given — the server stores unknown keys
        # verbatim, which is what lets the SDK grow the summary without a
        # server release.
        assert json.loads(cap.request.content) == summary

    def test_list_parses_records_with_open_summaries(self):
        cap = _CapturingRegistry(
            {
                "build_id": _BUILD_ID,
                "summaries": [
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "build_id": _BUILD_ID,
                        "outcome": "lingered_out",
                        "summary": {
                            "outcome": "lingered_out",
                            "some_future_counter": 7,
                        },
                        "created_at": "2026-07-04T09:00:00+00:00",
                    }
                ],
            }
        )
        records = cap.registry.build_list_tick_summaries(UUID(_BUILD_ID), limit=5)
        assert cap.request.url.params["limit"] == "5"
        assert len(records) == 1
        assert records[0].outcome == "lingered_out"
        # The blob is kept whole, unknown keys included.
        assert records[0].summary["some_future_counter"] == 7
