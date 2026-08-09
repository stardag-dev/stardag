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
