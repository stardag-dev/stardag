"""Fixtures for the registry-backed CLI groups, driven against the
in-memory v2 registry (:class:`stardag.testing.InMemoryRegistry`)."""

from __future__ import annotations

import asyncio
import typing
from contextlib import ExitStack
from dataclasses import dataclass
from unittest import mock
from uuid import UUID

import pytest

from stardag.build._registration import new_id, register_plan_aio, walk_aio
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

# Every module that binds ``_resolve_registry`` into its own namespace.
CLI_MODULES = (
    "builds",
    "builds_stop",
    "builds_frontier",
    "builds_tick",
    "executions",
    "plans",
    "deployments",
    "tasks",
    "tasks_actions",
    "limits",
)


class SpawnRecorder:
    """Stands in for the Modal tick spawner: records, never reaches Modal.
    ``refuse`` plays a build on another Modal workspace."""

    def __init__(self) -> None:
        self.spawned: list[tuple[UUID, str]] = []
        self.fail_with: Exception | None = None
        self.refuse: str | None = None

    def for_build(self, build):
        if self.refuse is not None:
            return None, self.refuse
        return self._spawn, None

    def _spawn(self, build_id: UUID, app_name: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.spawned.append((build_id, app_name))


@pytest.fixture(autouse=True)
def tick_spawner() -> typing.Iterator[SpawnRecorder]:
    """Every CLI write now wakes the builds it changed, and this machine may
    well have Modal credentials: no CLI test may spawn a real tick. A test
    that wants no spawner patches ``modal_spawner`` to return None."""
    recorder = SpawnRecorder()
    with ExitStack() as stack:
        for module in ("_wake", "builds_tick"):
            stack.enter_context(
                mock.patch(
                    f"stardag._cli.{module}.modal_spawner", return_value=recorder
                )
            )
        yield recorder


@pytest.fixture
def fake_registry() -> typing.Iterator[InMemoryRegistry]:
    """An in-memory registry every CLI group resolves to."""
    registry = InMemoryRegistry()
    with ExitStack() as stack:
        for module in CLI_MODULES:
            stack.enter_context(
                mock.patch(
                    f"stardag._cli.{module}._resolve_registry",
                    return_value=registry,
                )
            )
        yield registry


@dataclass
class RunningBuild:
    build_id: UUID
    plan_id: UUID
    deployment_id: UUID
    leaf: SyncOnlyTask
    root: SyncOnlyTask
    execution_id: UUID


@pytest.fixture
def running_build(
    fake_registry: InMemoryRegistry,
    default_in_memory_fs_target,
    monkeypatch: pytest.MonkeyPatch,
) -> RunningBuild:
    """A sealed plan of two tasks (leaf -> root), the leaf claimed by a Modal
    execution that has not reported its end."""
    monkeypatch.setenv("STARDAG_CODE_ID", "cli-test")
    registry = fake_registry
    deployment = registry.add_deployment(kind="local", code_id="cli-test")
    leaf = SyncOnlyTask(name="cli-leaf")
    root = SyncOnlyTask(name="cli-root", deps=(leaf,))
    build_id = registry.build_create(root_task_ids=[str(root.id)]).id
    walk = asyncio.run(walk_aio([root]))
    plan = asyncio.run(
        register_plan_aio(
            registry, build_id, walk, deployment_id=deployment, settings={}
        )
    )
    execution_id = new_id()
    registry.member_start(
        plan.id,
        str(leaf.id),
        execution_id=execution_id,
        executor="modal",
        executor_ref="fc-leaf",
        executor_metadata={"function_name": "worker_default", "workspace": "ws"},
    )
    return RunningBuild(
        build_id=build_id,
        plan_id=plan.id,
        deployment_id=deployment,
        leaf=leaf,
        root=root,
        execution_id=execution_id,
    )
