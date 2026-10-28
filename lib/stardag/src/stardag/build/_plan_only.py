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

:func:`register` adds the roots' handles: per root, a
:class:`~stardag.TaskRef` pinned by task id and instance id, and the
``ref(...)`` line printed for it (the shared reference-line helper's).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from stardag._core.base_task import BaseTask
from stardag._core.task_ref import (
    TaskRef,
    reference_call,
    reference_line,
    resolve_refs_aio,
    task_ref,
)
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
class RegisteredTask:
    """One root of a plan-only build, as the registry recorded it: the task,
    its ids, and the lines of code that pin it (from
    :func:`~stardag._core.task_ref.reference_line`, shared with ``stardag
    tasks ref``)."""

    task: BaseTask
    task_id: str
    #: The root's instance under the build's scope: the body a reference
    #: pinned to it hydrates from.
    instance_id: UUID
    #: ``Cls.ref("<task_id>", instance_id="<instance_id>")``, as printed.
    ref_line: str
    #: ``from <module> import <name>``, or a ``#`` note when the class cannot
    #: be imported by name from other code.
    import_line: str

    @classmethod
    def of(cls, task: BaseTask, instance_id: UUID) -> "RegisteredTask":
        task_class = _origin(type(task))
        importable, prefix = reference_line(task_class)
        if importable is None:
            where = (
                "the script being run (__main__)"
                if task_class.__module__ == "__main__"
                else f"{task_class.__module__}, but is not importable by name"
            )
            importable = (
                f"# {prefix} is defined in {where}: move it to a module to "
                "reference it from other code"
            )
        return cls(
            task=task,
            task_id=str(task.id),
            instance_id=instance_id,
            ref_line=reference_call(prefix, task_class, str(task.id), str(instance_id)),
            import_line=importable,
        )

    @property
    def ref(self) -> TaskRef:
        """The typed reference pinning this root
        (``TaskRef[TaskClass, LoadedT]``)."""
        return task_ref(_origin(type(self.task)), self.task_id, self.instance_id)


def _origin(cls: type) -> type:
    """A parametrised generic task (``GenericTask[int]``) is a pydantic
    subclass named with its arguments; code refers to the declared origin."""
    while origin := getattr(cls, "__pydantic_generic_metadata__", {}).get("origin"):
        cls = origin
    return cls


@dataclass(frozen=True)
class PlanOnlyResult:
    """What a plan-only build registered."""

    build_id: UUID
    plan_id: UUID
    #: The distinct roots, in request order.
    roots: list[RegisteredTask]
    task_count: TaskCount
    #: One per given task, in order (a repeated task repeats its root): what
    #: ``sd.register`` returns, as references.
    requested: list[RegisteredTask]

    def summary(self) -> BuildSummary:
        return BuildSummary(
            status=BuildExitStatus.REGISTERED,
            task_count=self.task_count,
            build_id=self.build_id,
        )


def _check_tasks(tasks: Sequence[object]) -> None:
    for index, task in enumerate(tasks):
        if not isinstance(task, BaseTask):
            raise ValueError(
                f"Invalid task at index {index}: {task!r} (must be BaseTask)"
            )


def _distinct(roots: Sequence[BaseTask]) -> list[BaseTask]:
    """The roots with identical instances coalesced. Two constructions of
    one task id that differ (a non-significant parameter) are both kept, so
    the walk refuses them (``InstanceConflictError``) before any build
    exists."""
    _check_tasks(roots)
    seen: dict[tuple[UUID, UUID], BaseTask] = {}
    for root in roots:
        seen.setdefault((root.id, root.instance_hash), root)
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
    registry = registry if registry is not None else registry_provider.get()
    given = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
    _check_tasks(given)
    # Every reference in the DAG becomes its hydrated task before anything is
    # read, walked or registered, as in an ordinary build.
    hydrated: list[BaseTask] = list(await resolve_refs_aio(given, registry))
    roots = _distinct(hydrated)
    if not roots:
        raise ValueError("A plan-only build needs at least one task.")
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
    by_key = {(r.task_id, str(r.task.instance_hash)): r for r in registered}
    return PlanOnlyResult(
        build_id=session.build_id,
        plan_id=session.plan_id,
        roots=registered,
        task_count=_count(walk),
        requested=[by_key[(str(t.id), str(t.instance_hash))] for t in hydrated],
    )


async def _registered_roots(
    registry: RegistryABC, plan_id: UUID, roots: Sequence[BaseTask]
) -> list[RegisteredTask]:
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
        registered.append(RegisteredTask.of(root, instance_id))
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
    task_description: str | None = None,
    registry: RegistryABC | None = None,
    app_name: str | None = None,
    settings: Mapping[str, str] | None = None,
    echo: bool = True,
) -> list[TaskRef]:
    """Register ``tasks`` without running them, and return a reference per
    root.

    A plan-only build (see :func:`build_plan_only_aio`): the tasks and
    their static closure are recorded under the scope, whether or not they
    are complete, and nothing is claimed or executed. A task given as a
    reference, or holding one, is hydrated first, as a build does. For each
    root it prints the import line and the reference that pins it::

        from my_project.models import TrainedModel
        TrainedModel.ref("<task_id>", instance_id="<instance_id>")

    Args:
        tasks: The root task(s), or references to them.
        tags: Set as each root's tags (replacing any it had). Tags annotate
            the completion (the task id), are mutable and outside every hash.
        task_description: Set as each root's description (named apart from
            ``sd.build``'s ``description``, which describes the build).
        registry: Default: the configured registry (one is required).
        app_name: Register under this deployed app's current deployment
            (default: a local deployment derived from the code).
        settings: Environment variables applied for the walk; part of the
            scope.
        echo: Print the handles (default).

    Returns:
        One :class:`~stardag.TaskRef` per given task, in order, pinned to the
        instance registered: ``.task_id``, ``.instance_id``, and
        ``.hydrate()`` for the task. Pass it wherever the task goes.

    Raises:
        APIError: An annotation was refused (an invalid tag, 422). The
            registration itself stands: the build is already REGISTERED.
    """
    result = await build_plan_only_aio(
        tasks, registry=registry, app_name=app_name, settings=settings
    )
    if tags is not None or task_description is not None:
        await _annotate(
            registry if registry is not None else registry_provider.get(),
            result.roots,
            tags=tags,
            task_description=task_description,
        )
    if echo:
        print(f"Registered build {result.build_id} (nothing was run):")
        for root in result.roots:
            print(f"  {root.import_line}")
            print(f"  {root.ref_line}")
    return [r.ref for r in result.requested]


async def _annotate(
    registry: RegistryABC,
    roots: Sequence[RegisteredTask],
    *,
    tags: Sequence[str] | None,
    task_description: str | None,
) -> None:
    """``PATCH /tasks/{task_id}`` per root; ``...`` leaves a field as is."""
    for root in roots:
        await asyncio.to_thread(
            registry.task_annotate,
            root.task_id,
            tags=list(tags) if tags is not None else ...,
            description=task_description if task_description is not None else ...,
        )


def register(
    tasks: Sequence[BaseTask] | BaseTask,
    *,
    tags: Sequence[str] | None = None,
    task_description: str | None = None,
    registry: RegistryABC | None = None,
    app_name: str | None = None,
    settings: Mapping[str, str] | None = None,
    echo: bool = True,
) -> list[TaskRef]:
    """Register tasks without running them (sync wrapper for
    :func:`register_aio`; see it for the arguments)."""
    try:
        return asyncio.run(
            register_aio(
                tasks,
                tags=tags,
                task_description=task_description,
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
