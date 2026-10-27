"""Plan-only builds: register tasks without running them.

A plan-only build registers its roots and their full static closure under
its scope ``(deployment, settings)``, exactly as an ordinary build's static
phase does, and then ends without anything ever being claimable:

1. The walk expands every task, complete or not (``register_all``), so every
   instance lands with its edges and ``expanded_at`` set.
2. The build is created **PENDING** with ``plan_only`` set
   (:meth:`ResidentSession.open`). The registry hands out work — frontier,
   claiming starts, wake-ups, ``stalled-builds`` — only for RUNNING builds,
   and a plan-only build is never RUNNING.
3. The plan is registered and sealed (:meth:`ResidentSession.register`).
4. ``POST /builds/{id}/registered`` ends it **REGISTERED**, a terminal
   status that ``resume`` and the reactive marker refuse.

Registration is an ordinary observation: a target found complete is
recorded COMPLETED, a recorded completion whose target is gone is
invalidated, and either wakes the reactive builds holding the task.

A plan-only build never carries on without the registry — whatever
``on_registry_failure`` says, since a registration that registers nothing
has done nothing — and an older registry that does not know plan-only
builds refuses the request (422), raised as
:class:`~stardag.exceptions.PlanOnlyUnsupportedError`. A failure after the
build was created fails it, best effort; one that cannot be recorded leaves
it PENDING, which no consumer acts on.

:func:`register` adds the roots' handles: per root, the ``ref(...)`` line
that pins it by task id and instance id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from stardag._core.base_task import BaseTask
from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    TaskCount,
    TaskExecutorABC,
)
from stardag.build._registration import (
    DEFAULT_MAX_CONCURRENT_DISCOVER,
    Walk,
    walk_aio,
)
from stardag.build._session import ResidentSession
from stardag.build._settings import resident_settings, resolve_settings_aio
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredRoot:
    """One root of a plan-only build, as the registry recorded it."""

    task: BaseTask
    task_id: str
    #: The root's instance under the build's scope: the body a reference
    #: pinned to it hydrates from.
    instance_id: UUID

    @property
    def import_line(self) -> str:
        cls = type(self.task)
        return f"from {cls.__module__} import {cls.__qualname__}"

    @property
    def ref_snippet(self) -> str:
        """The reference that pins this root: ``Cls.ref(task_id,
        instance_id=...)``."""
        return (
            f'{type(self.task).__qualname__}.ref("{self.task_id}", '
            f'instance_id="{self.instance_id}")'
        )


@dataclass(frozen=True)
class PlanOnlyResult:
    """What a plan-only build registered."""

    build_id: UUID
    plan_id: UUID
    #: The distinct roots, in request order.
    roots: list[RegisteredRoot]
    task_count: TaskCount

    def summary(self) -> BuildSummary:
        return BuildSummary(
            status=BuildExitStatus.REGISTERED,
            task_count=self.task_count,
            build_id=self.build_id,
        )


def _distinct(roots: Sequence[BaseTask]) -> list[BaseTask]:
    seen: dict[UUID, BaseTask] = {}
    for root in roots:
        seen.setdefault(root.id, root)
    return list(seen.values())


def _count(walk: Walk) -> TaskCount:
    return TaskCount(
        discovered=len(walk.order),
        previously_completed=len(walk.previously_completed),
    )


async def build_plan_only_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    *,
    registry: RegistryABC | None = None,
    task_executor: TaskExecutorABC | None = None,
    app_name: str | None = None,
    settings: Mapping[str, str] | None = None,
    description: str | None = None,
    max_concurrent_discover: int = DEFAULT_MAX_CONCURRENT_DISCOVER,
) -> PlanOnlyResult:
    """Register ``tasks`` and their static closure; run nothing.

    The scope is resolved as for an ordinary build: the deployed app's
    current deployment when ``app_name`` (or ``task_executor``'s
    ``deployment_app_name()``) names one, else a local deployment derived
    from the code; ``settings`` are applied for the walk and recorded with
    the plan.

    Raises:
        RuntimeError: No registry is configured.
        PlanOnlyUnsupportedError: The registry predates plan-only builds.
        InstanceConflictError, RequiresError, UnstableSerializationError:
            The walk failed; nothing was created.
        APIError: The registry refused (the build is failed, best effort).
    """
    roots = _distinct([tasks] if isinstance(tasks, BaseTask) else list(tasks))
    for index, task in enumerate(roots):
        if not isinstance(task, BaseTask):
            raise ValueError(
                f"Invalid task at index {index}: {task} (must be BaseTask)"
            )
    if not roots:
        raise ValueError("A plan-only build needs at least one task.")
    registry = registry if registry is not None else registry_provider.get()
    if app_name is None and task_executor is not None:
        app_name = task_executor.deployment_app_name()
    checked_settings = await resolve_settings_aio(registry, None, settings)
    session = ResidentSession(
        registry, on_registry_failure="raise", settings=checked_settings
    )
    with resident_settings(checked_settings):
        # Before anything is sent: a conflict or an unstable body fails here,
        # with no build created for it.
        walk = await walk_aio(
            roots,
            max_concurrent_discover=max_concurrent_discover,
            register_all=True,
        )
        await session.open(
            roots, app_name=app_name, description=description, plan_only=True
        )
        assert session.build_id is not None
        try:
            await session.register(walk)
            assert session.plan_id is not None
            await registry.build_registered_aio(session.build_id)
        except Exception as e:
            await _fail_best_effort(registry, session.build_id, e)
            raise
    registered = await _registered_roots(registry, session.plan_id, roots)
    return PlanOnlyResult(
        build_id=session.build_id,
        plan_id=session.plan_id,
        roots=registered,
        task_count=_count(walk),
    )


async def _registered_roots(
    registry: RegistryABC, plan_id: UUID, roots: Sequence[BaseTask]
) -> list[RegisteredRoot]:
    """The roots' instance ids, from the plan's root members (matched on
    task id and instance hash: a scope may hold several instances of one
    completion)."""
    members = await registry.plan_roots_aio(plan_id)
    by_key = {(m.task_id, m.instance_hash): m.instance_id for m in members}
    registered = []
    for root in roots:
        instance_id = by_key.get((str(root.id), str(root.instance_hash)))
        if instance_id is None:
            raise RuntimeError(
                f"The registry did not report root {root.id} as a member of "
                f"plan {plan_id} with this instance; it was registered, but "
                "its instance id is unknown."
            )
        registered.append(
            RegisteredRoot(task=root, task_id=str(root.id), instance_id=instance_id)
        )
    return registered


async def _fail_best_effort(
    registry: RegistryABC, build_id: UUID, error: BaseException
) -> None:
    try:
        await registry.build_fail_aio(build_id, f"{type(error).__name__}: {error}")
    except Exception as e:  # the original error is what the caller sees
        logger.warning(
            f"Could not record the failure of plan-only build {build_id}; it "
            f"stays PENDING, which nothing acts on: {e}"
        )


# -----------------------------------------------------------------------------
# sd.register
# -----------------------------------------------------------------------------


async def register_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    *,
    tags: Sequence[str] | None = None,
    description: str | None = None,
    registry: RegistryABC | None = None,
    app_name: str | None = None,
    settings: Mapping[str, str] | None = None,
    echo: bool = True,
) -> list[UUID]:
    """Register ``tasks`` without running them, and print a handle per root.

    A plan-only build (see :func:`build_plan_only_aio`): the tasks and
    their static closure are recorded under the scope, whether or not they
    are complete, and nothing is claimed or executed. For each root it
    prints the import line and the reference that pins it::

        from my_project.models import TrainedModel
        TrainedModel.ref("<task_id>", instance_id="<instance_id>")

    Args:
        tasks: The root task(s).
        tags: Not available yet (annotating the roots).
        description: Not available yet (annotating the roots).
        registry: Default: the configured registry (one is required).
        app_name: Register under this deployed app's current deployment
            (default: a local deployment derived from the code).
        settings: Environment variables applied for the walk; part of the
            scope.
        echo: Print the handles (default).

    Returns:
        The roots' instance ids, one per given task, in order.
    """
    # TODO(STA-149): annotate the roots (tags, description) once the
    # registry's task annotations land; until then these are refused rather
    # than silently dropped.
    if tags is not None or description is not None:
        raise NotImplementedError(
            "sd.register(tags=..., description=...) is not available yet: "
            "annotating registered tasks needs registry support that has not "
            "shipped. Register without them."
        )
    task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
    result = await build_plan_only_aio(
        task_list, registry=registry, app_name=app_name, settings=settings
    )
    by_task_id = {r.task_id: r for r in result.roots}
    if echo:
        print(f"Registered build {result.build_id} (nothing was run):")
        for root in result.roots:
            print(f"  {root.import_line}")
            print(f"  {root.ref_snippet}")
    return [by_task_id[str(t.id)].instance_id for t in task_list]


def register(
    tasks: Sequence[BaseTask] | BaseTask,
    *,
    tags: Sequence[str] | None = None,
    description: str | None = None,
    registry: RegistryABC | None = None,
    app_name: str | None = None,
    settings: Mapping[str, str] | None = None,
    echo: bool = True,
) -> list[UUID]:
    """Register tasks without running them (sync wrapper for
    :func:`register_aio`; see it for the arguments)."""
    try:
        return asyncio.run(
            register_aio(
                tasks,
                tags=tags,
                description=description,
                registry=registry,
                app_name=app_name,
                settings=settings,
                echo=echo,
            )
        )
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            raise RuntimeError(
                "register() cannot be used from within an already running event "
                "loop. Use 'await register_aio()' instead."
            ) from e
        raise
