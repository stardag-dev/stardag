"""The ``task_start_claim_aio`` contract on the registry base classes.

Exactly-once arbitration has no safe default: a backend that answers
"you won" without arbitrating is indistinguishable from one that
arbitrates correctly. So :class:`RegistryABC` refuses, and
:class:`NoOpRegistry` — the registry-*less* path, where there is no shared
state to arbitrate against in the first place — opts out explicitly.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from stardag import BaseTask
from stardag.registry import NoOpRegistry, RegistryABC
from stardag.registry._base import TaskMetadata


def _make_task() -> BaseTask:
    task = MagicMock(spec=BaseTask)
    task.id = uuid4()
    return task


class BareRegistry(RegistryABC):
    """Implements only the two abstract methods."""

    def task_register(self, build_id, task) -> None:
        pass

    def task_get_metadata(self, task_id) -> TaskMetadata:
        raise NotImplementedError


class TestTaskStartClaimAio:
    async def test_registry_abc_refuses(self):
        with pytest.raises(NotImplementedError, match="task_start_claim_aio"):
            await BareRegistry().task_start_claim_aio(uuid4(), _make_task())

    async def test_noop_registry_grants(self):
        result = await NoOpRegistry().task_start_claim_aio(
            uuid4(), _make_task(), limit_keys=["gpu"]
        )
        assert result.started is True
        assert result.denied_reason is None


class TestApiRegistryStartFlags:
    """``claim`` and ``enforce_limits`` are orthogonal flags on the API's one
    ``/start`` endpoint, and one SDK method reaches both.

    ``claim=False`` is not a weaker claim, it is the absence of one, and it
    exists for exactly one caller: the resident build's
    ``RegistryConcurrencyLimiter``, which acquires slots for a task its own
    build has already claimed. A claiming acquire there is denied
    ``already_running`` by that very claim and polls until ``max_wait``, and
    nothing in the limiter's own behaviour would show which flags went out —
    hence a test on the query string.
    """

    def _registry_capturing(self, captured: list):
        import asyncio

        import httpx

        from stardag.registry._api_registry import APIRegistry

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        registry = APIRegistry(api_url="http://test.invalid", api_key="test-key")
        registry._async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=registry._auth,
        )
        registry._async_client_loop = asyncio.get_running_loop()
        return registry

    async def test_claiming_start_sends_claim_and_limits(self):
        captured: list = []
        registry = self._registry_capturing(captured)

        result = await registry.task_start_claim_aio(
            uuid4(), _make_task(), limit_keys=["gpu"]
        )

        assert result.started is True
        (request,) = captured
        assert request.url.params.get("claim") == "true"
        assert request.url.params.get("enforce_limits") == "true"
        assert request.url.params.get_list("limit_key") == ["gpu"]

    async def test_unclaiming_start_omits_claim_entirely(self):
        captured: list = []
        registry = self._registry_capturing(captured)

        result = await registry.task_start_claim_aio(
            uuid4(), _make_task(), limit_keys=["gpu"], claim=False
        )

        assert result.started is True
        (request,) = captured
        assert "claim" not in request.url.params, (
            "the flag is omitted rather than sent false, mirroring how "
            "enforce_limits is sent: the endpoint defaults both to off, so "
            "an unclaiming start is the plain start it was before"
        )
        assert request.url.params.get("enforce_limits") == "true"
