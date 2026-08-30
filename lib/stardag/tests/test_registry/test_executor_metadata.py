"""Tests for the optional ``executor_metadata`` registry surface.

Two layers:

- ``RegistryABC``'s async default implementations forward the kwarg to
  their sync counterparts.
- ``APIRegistry`` puts the dict on the wire: a JSON-encoded query param
  on the (body-less) task-start and build-resume endpoints, a body field
  on build creation.
"""

import gzip
import json
from unittest.mock import MagicMock
from uuid import uuid4

import httpx

from stardag import BaseTask
from stardag.registry._api_registry import APIRegistry
from stardag.registry._base import NoOpRegistry

METADATA = {
    "kind": "modal",
    "app_name": "demo-app",
    "workspace": "acme",
    "environment": "prod",
    "function_name": "worker_default",
}


def _make_task() -> BaseTask:
    task = MagicMock(spec=BaseTask)
    task.id = uuid4()
    return task


class MetadataAwareRegistry(NoOpRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def task_start(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ) -> None:
        self.calls.append(
            (
                "task_start",
                {
                    "executor": executor,
                    "executor_ref": executor_ref,
                    "executor_metadata": executor_metadata,
                },
            )
        )

    def build_start(self, root_tasks=None, description=None, executor_metadata=None):
        self.calls.append(("build_start", {"executor_metadata": executor_metadata}))
        return uuid4()

    def build_resume(self, build_id, executor_metadata=None) -> None:
        self.calls.append(("build_resume", {"executor_metadata": executor_metadata}))


class TestRegistryABCForwarding:
    async def test_task_start_aio_forwards_metadata(self):
        registry = MetadataAwareRegistry()
        await registry.task_start_aio(
            uuid4(),
            _make_task(),
            executor="modal",
            executor_ref="fc-1",
            executor_metadata=METADATA,
        )
        assert registry.calls == [
            (
                "task_start",
                {
                    "executor": "modal",
                    "executor_ref": "fc-1",
                    "executor_metadata": METADATA,
                },
            )
        ]

    async def test_build_start_aio_forwards_metadata(self):
        aware = MetadataAwareRegistry()
        await aware.build_start_aio(executor_metadata=METADATA)
        assert aware.calls == [("build_start", {"executor_metadata": METADATA})]

    async def test_build_resume_aio_forwards_metadata(self):
        aware = MetadataAwareRegistry()
        await aware.build_resume_aio(uuid4(), executor_metadata=METADATA)
        assert aware.calls == [("build_resume", {"executor_metadata": METADATA})]


class TestAPIRegistryWireFormat:
    def _make_registry_and_capture(self, response_json: dict, status_code: int = 200):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(status_code, json=response_json)

        registry = APIRegistry(api_url="http://test.invalid", api_key="test-key")
        registry._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            auth=registry._auth,
        )
        return registry, captured

    def test_task_start_sends_metadata_query_param(self, monkeypatch):
        monkeypatch.setenv("SHORT_SHA", "abc1234")
        registry, captured = self._make_registry_and_capture(
            {"task_id": "t", "status": "running"}
        )
        task = _make_task()

        registry.task_start(
            uuid4(),
            task,
            executor="modal",
            executor_ref="fc-1",
            executor_metadata=METADATA,
        )

        params = dict(httpx.QueryParams(captured[0].url.query))
        assert params["executor"] == "modal"
        assert params["executor_ref"] == "fc-1"
        assert json.loads(params["executor_metadata"]) == METADATA

    def test_task_start_omits_param_when_none(self, monkeypatch):
        monkeypatch.setenv("SHORT_SHA", "abc1234")
        registry, captured = self._make_registry_and_capture(
            {"task_id": "t", "status": "running"}
        )

        registry.task_start(uuid4(), _make_task(), executor="modal")

        params = dict(httpx.QueryParams(captured[0].url.query))
        assert "executor_metadata" not in params

    def test_build_start_sends_metadata_in_body(self, monkeypatch):
        monkeypatch.setenv("SHORT_SHA", "abc1234")
        build_id = uuid4()
        registry, captured = self._make_registry_and_capture(
            {"id": str(build_id), "name": "brave-tiger-42"}, status_code=201
        )

        result = registry.build_start(executor_metadata=METADATA)

        assert result == build_id
        raw = captured[0].content
        if captured[0].headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        body = json.loads(raw)
        assert body["executor_metadata"] == METADATA

    def test_build_resume_sends_metadata_query_param(self, monkeypatch):
        monkeypatch.setenv("SHORT_SHA", "abc1234")
        build_id = uuid4()
        registry, captured = self._make_registry_and_capture(
            {"id": str(build_id), "name": "brave-tiger-42"}
        )

        registry.build_resume(build_id, executor_metadata=METADATA)

        params = dict(httpx.QueryParams(captured[0].url.query))
        assert json.loads(params["executor_metadata"]) == METADATA


class TestBulkRegisterResponseParsing:
    def test_parses_latest_executor_metadata(self):
        from stardag.registry._api_registry import _parse_bulk_register_response

        infos = _parse_bulk_register_response(
            {
                "tasks": [
                    {
                        "id": str(uuid4()),
                        "task_id": "t-1",
                        "latest_status": "running",
                        "latest_executor": "modal",
                        "latest_executor_ref": "fc-1",
                        "latest_executor_metadata": METADATA,
                    },
                    # Older server: field absent → None.
                    {"id": str(uuid4()), "task_id": "t-2"},
                ]
            }
        )
        assert infos is not None
        assert infos[0].latest_executor_metadata == METADATA
        assert infos[1].latest_executor_metadata is None
