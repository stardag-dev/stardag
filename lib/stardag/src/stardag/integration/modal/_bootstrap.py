"""Reactive bootstrap: everything a reactive build needs before it can tick.

:func:`run_reactive_bootstrap` discovers the DAG, refuses it if a scheduler
tick could not rebuild every task in it, arms the build and spawns the first
tick. It normally runs **inside Modal**, as the body of the deployed
``bootstrap`` function (discovery is target-root I/O, which is far cheaper
next to a mounted volume than from a laptop), and runs in the triggering
process instead when an app opts out with
``StardagApp(reactive_discovery="local")``.

Also here: the rehydration pre-flight the bootstrap applies (and the
advisory, roots-only version the trigger emits), and
:func:`_fail_build_best_effort`, the "never leave an orphan RUNNING build"
helper its callers share.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from uuid import UUID

import modal

from stardag import BaseTask
from stardag.build._deployment import resolve_deployment_id_aio
from stardag.build._registration import Walk, register_plan_aio, walk_aio
from stardag.build._settings import settings_applied
from stardag.build._task_modules import (
    RehydrationPlan,
    TaskModulesError,
    expand_task_module_patterns,
    import_task_modules,
    format_uncovered_message,
    plan_rehydration,
    uncovered_task_classes,
)

logger = logging.getLogger(__name__)


def _preflight_rehydration(
    build_id: UUID,
    tasks: typing.Iterable[BaseTask],
    task_module_patterns: typing.Sequence[str],
) -> None:
    """Refuse to arm a build a scheduler tick could not drive. Raises.

    Takes the app's declared ``task_modules`` **patterns**, not the module
    list they expand to: coverage is a question about patterns, and
    :func:`plan_rehydration` matches classes against them. (The expansion
    is a separate deploy-time artifact — it is what a tick imports; see
    :class:`stardag.integration.modal._tick._TickDeployment`.)

    **The authoritative check, and the only one.** It runs wherever
    discovery runs — normally the bootstrap container — on the set
    discovery just walked: those are exactly the tasks a tick will have to
    rebuild, and reusing discovery's (pruned) walk avoids a second
    traversal of the DAG.

    **It raises.** A tick has no second way to get a task object than its
    instance body, so a task that fails the dry run is one the build can
    never schedule. The alternatives are both worse than a
    refusal at trigger time: arming the build anyway means it runs until it
    reaches that task and then fails it, hours later, one task at a time;
    warning and continuing means the same thing with a log line nobody
    reads. Here the message names every offending class at once, with the
    ``task_modules`` entry that would cover it, before anything is spawned.

    In the default (bootstrap) placement these are the patterns **baked
    into the deployment at ``finalize()``** — not the caller's local app
    definition. So "you changed ``task_modules`` but didn't redeploy" is
    visible rather than silently agreeable.
    """
    plan: RehydrationPlan = plan_rehydration(tasks, task_module_patterns)
    error = plan.error(task_module_patterns)
    if error is not None:
        raise TaskModulesError(error)
    logger.info(f"Build {build_id} rehydration pre-flight: {plan.summary()}")


def _advise_uncovered_root_task_modules(
    root_tasks: typing.Sequence[BaseTask], task_module_patterns: typing.Sequence[str]
) -> None:
    """Advisory, roots-only coverage note emitted at the trigger.

    Takes the declared ``task_modules`` **patterns**, like
    :func:`_preflight_task_modules`.

    Purely additive early feedback, and deliberately **not** a check in
    its own right: :func:`_preflight_rehydration` is the authoritative
    one and always runs over the full discovered set wherever discovery
    runs. This looks at the **root tasks only** — a fixed, tiny set the
    trigger already holds — so it costs no ``requires()`` traversal, no
    target I/O and no measurable time, and it is by construction a
    *subset* of what the real check sees. Two checks that can disagree
    would be worse than one; a subset can only ever be quieter.

    Why it earns its place anyway: the dominant ``task_modules``
    misconfiguration is "I never declared my package", and in that case
    the roots are uncovered too. Saying so in the operator's terminal, at
    the moment they trigger, beats saying it a container start later in a
    log they have to go and find.
    """
    if not task_module_patterns or not root_tasks:
        return
    uncovered = uncovered_task_classes(root_tasks, task_module_patterns)
    if not uncovered:
        return
    logger.warning(
        format_uncovered_message(
            uncovered,
            task_module_patterns,
            remedy=(
                "This is an early, ROOT-TASKS-ONLY note from the trigger; "
                "the full check runs over the whole discovered DAG where "
                "discovery runs, may name more classes, and REFUSES the "
                "build rather than warning."
            ),
        )
    )


def _fail_build_best_effort(
    registry: typing.Any, build_id: UUID, exception: BaseException
) -> None:
    """Record a terminal BUILD_FAILED for ``build_id``, never raising.

    The caller is already propagating ``exception``; this exists only so
    the propagation doesn't leave a build sitting RUNNING forever with
    nothing driving it. A failure to record the failure is logged and
    swallowed — masking the real cause with a registry error would be a
    strictly worse outcome.
    """
    try:
        registry.build_fail(
            build_id,
            error_message=f"{type(exception).__name__}: {exception}",
        )
    except Exception:
        logger.exception(
            f"Could not record BUILD_FAILED for build {build_id} after "
            f"{type(exception).__name__}; the build may be left RUNNING "
            "with nothing driving it (re-trigger it, or cancel it from "
            "the UI)."
        )


ReactiveDiscovery = typing.Literal["modal", "local"]
"""Where a reactive trigger discovers the DAG (see ``StardagApp``).

``"modal"`` (the default) spawns the deployed ``bootstrap`` function;
``"local"`` runs the identical bootstrap in the triggering process.
"""


class ReactiveBootstrapResult(typing.NamedTuple):
    """Result of :func:`run_reactive_bootstrap`.

    Attributes:
        summary: JSON-able account of what the bootstrap did. This is what
            the deployed ``bootstrap`` function returns to Modal.
        tick_call: The ``FunctionCall`` handle of the first scheduler tick
            the bootstrap spawned. Only useful in-process (it does not
            survive a Modal return value), so the deployed function drops
            it and the local-discovery trigger path keeps it.
    """

    summary: dict[str, typing.Any]
    tick_call: typing.Any


def run_reactive_bootstrap(
    build_id: UUID,
    task_list: list[BaseTask],
    *,
    registry: typing.Any,
    app_name: str,
    tick_kwargs: dict[str, typing.Any] | None,
    task_module_patterns: typing.Sequence[str],
    settings: typing.Mapping[str, str] | None = None,
) -> ReactiveBootstrapResult:
    """Plan the build, check it, arm it, spawn the first tick.

    **The static phase.** The bootstrap plans under its own deployment —
    ``STARDAG_DEPLOYMENT_ID``, baked into the container by the deploy — or,
    run in the triggering process (``reactive_discovery="local"``), under
    the app's current deployment (D13). It applies the build's ``settings``,
    walks the roots (stopping at complete tasks), refuses the build if a
    tick could not rehydrate every task it walked, and registers the plan:
    roots first, the rest in post-order chunks, then ``/seal``
    (:func:`~stardag.build._registration.register_plan_aio`). A re-trigger
    under the same scope reuses the build's plan: the observations are
    re-sent (a vanished output is invalidated) and members that failed,
    were cancelled, skipped, suspended or interrupted are reset — the retry
    path of a reactive build.

    Normally runs **inside Modal**, as the body of the deployed
    ``bootstrap`` function, because discovery is target-root I/O (a mounted
    volume there, a rate-limited API from a laptop).

    **The ordering guarantee.** The reactive marker
    (``build_set_reactive_meta``) is written **last**, after the plan is
    sealed, and a tick no-ops on a build without it; the first tick is
    spawned after. (A plan registered roots-first is recoverable at any
    point anyway — its unexpanded roots are discovery jobs any tick can
    finish.)

    Raises on any failure without touching the build's status: recording
    BUILD_FAILED belongs to the caller, which knows whether *it* put the
    build into RUNNING.
    """
    with settings_applied(settings, owner=build_id):
        if task_module_patterns:
            import_task_modules(expand_task_module_patterns(task_module_patterns))
        walk = asyncio.run(
            _plan_aio(
                registry,
                build_id,
                task_list,
                app_name=app_name,
                settings=dict(settings or {}),
                task_module_patterns=task_module_patterns,
            )
        )
    registry.build_set_reactive_meta(
        build_id, app_name=app_name, tick_kwargs=tick_kwargs
    )
    tick_function = modal.Function.from_name(app_name=app_name, name="tick")
    tick_call = tick_function.spawn(build_id=str(build_id))
    summary = {
        "build_id": str(build_id),
        "roots": len(task_list),
        "incomplete": len(walk.incomplete),
        "previously_completed": len(walk.previously_completed),
    }
    logger.info(f"Reactive bootstrap for build {build_id}: {summary}")
    return ReactiveBootstrapResult(summary=summary, tick_call=tick_call)


async def _plan_aio(
    registry: typing.Any,
    build_id: UUID,
    roots: typing.Sequence[BaseTask],
    *,
    app_name: str,
    settings: dict[str, str],
    task_module_patterns: typing.Sequence[str],
) -> Walk:
    deployment_id = await resolve_deployment_id_aio(registry, app_name=app_name)
    walk = await walk_aio(list(roots))
    _preflight_rehydration(build_id, walk.incomplete, task_module_patterns)
    # Reactivates this scope's plan if the build moved away from it since.
    await registry.build_resume_aio(
        build_id, deployment_id=deployment_id, settings=settings
    )
    await register_plan_aio(
        registry,
        build_id,
        walk,
        deployment_id=deployment_id,
        settings=settings,
        retry_failed=True,
    )
    return walk
