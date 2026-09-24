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
    "executions",
    "plans",
    "deployments",
    "tasks",
    "limits",
)


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
