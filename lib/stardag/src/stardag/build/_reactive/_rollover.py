"""Rollover: a tick of a newer deployment re-plans the build under it.

A build follows the live deployment (design.md, "Rollover"). A tick reads
its own ``STARDAG_DEPLOYMENT_ID`` and compares it with the active plan's
``deployment_id`` — an id comparison. If they differ:

1. It checks the registry's current deployment of the app is its own; if
   not it exits ``superseded`` (rollover only moves forward, and only on
   the registry's record).
2. It rehydrates the active plan's root instances from their bodies under
   its own code, recomputes their task ids and compares them with the
   build's ``root_task_ids``; a difference fails the build with "re-trigger
   it as a new build".
3. It runs the static phase under ``(own deployment, plan settings)`` —
   lookup-or-create, so a plan another tick of this deployment already
   sealed is reused — and seals. ``/seal`` re-checks the deployment is
   still current, so two ticks under two new deployments cannot leave the
   build on the older code; the winner's seal supersedes the old plan.
"""

from __future__ import annotations

import logging
import typing
from collections.abc import Callable
from uuid import UUID

from stardag import BaseTask, task_from_registry_data
from stardag.build._deployment import (
    DeploymentResolutionError,
    current_app_deployment_id_aio,
)
from stardag.build._registration import Walk, register_plan_aio, walk_aio
from stardag.build._settings import settings_applied
from stardag.exceptions import APIError
from stardag.registry import BuildFrontier, RegistryABC

logger = logging.getLogger(__name__)

RollOverOutcome = typing.Literal["rolled", "superseded"]

#: Refusals of the re-plan meaning another deployment is (or became) the
#: current one: this tick is superseded, not failed.
_SUPERSEDED_CODES = frozenset({"plan_superseded", "deployment_not_current"})


class RollOverFailed(Exception):
    """The build cannot follow this code; it has been failed. ``payload``
    carries what the caller reports beside the summary."""

    def __init__(self, message: str, payload: dict[str, typing.Any]):
        super().__init__(message)
        self.payload = payload


RollOver = Callable[[BuildFrontier], typing.Awaitable[RollOverOutcome]]
"""A tick's rollover hook, called under the lease once per tick when the
active plan's deployment is not the tick's own, and only for a RUNNING
build. Returns ``"rolled"`` or ``"superseded"``; raises
:class:`RollOverFailed` when the build cannot follow this code."""


async def _fail(
    registry: RegistryABC, build_id: UUID, reason: str, error: BaseException
) -> typing.NoReturn:
    message = (
        f"Rollover of build {build_id} failed: {reason}: "
        f"{type(error).__name__}: {error}. Re-trigger it as a new build."
    )
    logger.error(message)
    try:
        await registry.build_fail_aio(build_id, message)
    except Exception:
        logger.exception(f"Could not record the failure of build {build_id}")
    raise RollOverFailed(
        message,
        {"outcome": "rollover_failed", "error": f"{type(error).__name__}: {error}"},
    ) from error


async def roll_over_aio(
    registry: RegistryABC,
    frontier: BuildFrontier,
    *,
    own_deployment_id: UUID,
    preflight: Callable[[Walk], None] | None = None,
    max_concurrent_discover: int = 16,
) -> RollOverOutcome:
    """Re-plan ``frontier``'s build under ``own_deployment_id``; see the
    module docstring. ``preflight`` checks the walk before anything is
    registered (the Modal integration's task-module coverage check)."""
    build_id = frontier.build_id
    app_name = frontier.reactive_app_name
    assert frontier.plan_id is not None and frontier.settings_hash is not None
    if app_name is None:
        return "superseded"
    try:
        current = await current_app_deployment_id_aio(registry, app_name)
    except DeploymentResolutionError:
        current = None
    if current != own_deployment_id:
        logger.info(
            f"Tick for build {build_id}: planned under another deployment, and "
            f"this tick's ({own_deployment_id}) is not the current one of "
            f"{app_name!r}; superseded."
        )
        return "superseded"
    build = await registry.build_get_aio(build_id)
    settings = (await registry.settings_get_aio(frontier.settings_hash)).body
    roots_members = await registry.plan_roots_aio(frontier.plan_id)
    with settings_applied(settings):
        try:
            roots: list[BaseTask] = [
                task_from_registry_data(m.body) for m in roots_members
            ]
        except Exception as e:
            await _fail(registry, build_id, "a root could not be rehydrated", e)
        root_ids = sorted({str(r.id) for r in roots})
        if root_ids != sorted(set(build.root_task_ids)):
            await _fail(
                registry,
                build_id,
                "the roots' task ids differ under this code",
                ValueError(f"{root_ids} != {sorted(set(build.root_task_ids))}"),
            )
        try:
            walk = await walk_aio(
                roots, max_concurrent_discover=max_concurrent_discover
            )
            if preflight is not None:
                preflight(walk)
        except Exception as e:
            await _fail(registry, build_id, "planning under this code failed", e)
        try:
            await register_plan_aio(
                registry,
                build_id,
                walk,
                deployment_id=own_deployment_id,
                settings=settings,
            )
        except APIError as e:
            if e.code in _SUPERSEDED_CODES:
                return "superseded"
            await _fail(registry, build_id, "registering the new plan failed", e)
    logger.info(
        f"Tick for build {build_id}: rolled over to deployment {own_deployment_id}"
        f" — {len(walk.incomplete)} incomplete task(s) planned, "
        f"{len(walk.previously_completed)} already complete."
    )
    return "rolled"
