"""A task planned and claimed in an :class:`InMemoryRegistry`, the way the
orchestrator leaves it before a worker container starts: a build, an
activated deployment, a sealed plan, and a claiming start under a
client-minted execution id. The worker's reports are then validated by the
fake's server seams (authority by execution, the claim's plan, the yield's
deployment check) rather than merely recorded."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from stardag import BaseTask
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.build._registration import new_id, register_plan_aio, walk_aio
from stardag.integration.modal._metadata import (
    STARDAG_BUILD_ID_ENV,
    STARDAG_EXECUTION_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_PLAN_ID_ENV,
    STARDAG_REACTIVE_ENV,
)
from stardag.testing import InMemoryRegistry


@dataclass
class Planned:
    registry: InMemoryRegistry
    task: BaseTask
    build_id: UUID
    plan_id: UUID
    deployment_id: UUID
    execution_id: UUID

    @property
    def task_id(self) -> str:
        return str(self.task.id)

    def env(self, *, reactive: bool = False, **extra: str) -> dict[str, str]:
        env = {
            STARDAG_BUILD_ID_ENV: str(self.build_id),
            STARDAG_PLAN_ID_ENV: str(self.plan_id),
            STARDAG_EXECUTION_ID_ENV: str(self.execution_id),
            STARDAG_DEPLOYMENT_ID_ENV: str(self.deployment_id),
        }
        if reactive:
            env[STARDAG_REACTIVE_ENV] = "1"
            env[STARDAG_MODAL_APP_NAME_ENV] = "app"
        env.update(extra)
        return env

    def reports(self) -> list[str]:
        """The worker's calls about this task, in order (the setup's
        claiming start excluded)."""
        return [
            method
            for method, kwargs in self.registry.calls
            if kwargs.get("task_id") == self.task_id
            and not (method == "member_start" and kwargs.get("claim", True))
        ]


def plan_and_claim(task: BaseTask, registry: InMemoryRegistry | None = None) -> Planned:
    registry = registry or InMemoryRegistry()
    deployment_id = registry.add_deployment(app_name="app")
    build_id = registry.build_create(root_task_ids=[str(task.id)]).id

    async def _plan():
        walk = await walk_aio([task])
        return await register_plan_aio(
            registry, build_id, walk, deployment_id=deployment_id, settings={}
        )

    plan = asyncio.run(_plan())
    execution_id = new_id()
    registry.member_start(plan.id, str(task.id), execution_id=execution_id)
    return Planned(registry, task, build_id, plan.id, deployment_id, execution_id)
