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
from stardag.build import discover_and_register_aio
from stardag.build._reactive._discovery import DiscoveryResult
from stardag.build._scope import code_id, structure_scope_key
from stardag.build_config import build_config_scope, rebind_to_build_config
from stardag.integration.modal._limit_keys import LimitKeySelector
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

    **It raises.** A tick has no second way to get a task object since the
    pickle store was retired, so a task that fails the dry run is one the
    build can never schedule. The alternatives are both worse than a
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
    limit_key_selector: LimitKeySelector | None = None,
    build_config: typing.Mapping[str, typing.Mapping[str, typing.Any]] | None = None,
) -> ReactiveBootstrapResult:
    """Discover the DAG, check it, arm the build, spawn the first tick.

    **First, the structure scope.** The bootstrap runs inside the
    deployment, so it knows the code id every tick and worker of this
    deployment will answer, and it holds the build config; it fixes the
    build's scope from the two *before* registering any edge, so every
    edge the build writes carries the scope of the code and config that
    evaluated it. A build planned by other code (a re-trigger from a new
    deployment) is simply re-planned here and its scope moved — see
    :func:`plan_under_scope_aio`. The config is installed
    for this process, and the roots — constructed on the triggering
    machine, before any config existed — are re-created under it, so
    their ``dependencies_only`` / ``execution_only`` fields, and those of
    every task ``requires()`` constructs from here on, come from the
    build config. See ``docs/design/scope-keyed-dependency-structure.md``.

    Everything a reactive build needs before it can be scheduled, except
    minting the build and registering its roots — those cost no target
    I/O and must happen at the trigger, before anything is spawned.

    Normally this runs **inside Modal**, as the body of the deployed
    ``bootstrap`` function, because discovery is target-root I/O:
    ``complete_aio()`` is one target existence check per task, and for a
    ``modalvol://`` root that is a rate-limited Volume *API* call from
    outside Modal versus a ``stat`` on a mounted filesystem inside it. The
    same code also runs at the trigger when an app opts out with
    ``StardagApp(reactive_discovery="local")``.

    **The ordering guarantee — do not "tidy" this.** The reactive marker
    (``build_set_reactive_meta``, which is what makes ``reactive_app_name``
    non-None) is written **last**, after discovery *and* registration have
    completed, and a tick no-ops on any build whose ``reactive_app_name``
    is None. That ordering is the whole reason no tick can ever observe a
    partially-registered DAG. It is load-bearing, not stylistic:
    registration is chunked post-order, so the roots land *last*, and
    mid-registration a build presents as "nothing actionable, roots not
    complete" — exactly the shape terminal detection fails a build on.
    Moving the marker earlier (or spawning a tick before it) reopens
    precisely that window.

    Raises on any failure without touching the build's status: recording
    the terminal BUILD_FAILED belongs to the caller, which is the one
    that knows whether *it* put the build into RUNNING (see
    :meth:`StardagApp._trigger_reactive`). The first tick's spawn is part
    of the work rather than an afterthought: an un-spawned tick is not a
    partial success, it is a build nothing will ever move (a watchdog
    would eventually adopt it; an app without one would simply stall).
    """
    # ``limit_key_selector`` rides along so every task is registered with
    # the concurrency-limit keys it will run under. The registry uses those
    # plan-time keys to wake the builds queued on a key when a slot frees —
    # it can learn them nowhere else, since the selector is deployed-app
    # code.
    # The config is installed for the duration of the bootstrap only: the
    # ``reactive_discovery="local"`` path runs this in the trigger's own
    # process, and a completed build's level 2/3 values must not linger in
    # the caller's context for the next task it constructs.
    with build_config_scope(build_config):
        return _run_reactive_bootstrap_scoped(
            build_id,
            task_list,
            registry=registry,
            app_name=app_name,
            tick_kwargs=tick_kwargs,
            task_module_patterns=task_module_patterns,
            limit_key_selector=limit_key_selector,
            build_config=build_config,
        )


async def plan_under_scope_aio(
    registry: typing.Any,
    build_id: UUID,
    roots: typing.Sequence[BaseTask],
    *,
    scope_key: str,
    build_config: typing.Mapping[str, typing.Mapping[str, typing.Any]] | None,
    task_module_patterns: typing.Sequence[str],
    limit_key_selector: LimitKeySelector | None,
    retry_failed: bool,
) -> DiscoveryResult:
    """Plan ``build_id`` under ``scope_key``: discover, register, check, move.

    The one planning step, shared by the reactive bootstrap (a fresh build)
    and a tick's **rollover** (a build the live deployment inherits from
    other code — see ``docs/design/scope-keyed-dependency-structure.md``).
    Discovery walks the roots under the config already installed in this
    process, registers every incomplete task with its static upstreams
    **explicitly under** ``scope_key`` — the scope of the code doing the
    walking — checks that a scheduler tick could rebuild every one of them
    from registry data, and only then moves the build's scope to
    ``scope_key``. Registering first and moving last means a scheduler that
    reads the build mid-plan still gates over the old, complete plan rather
    than a half-written new one.

    Raises :class:`TaskModulesError` if the pre-flight refuses the plan —
    for the bootstrap that fails the build at the trigger, and for a
    rollover the tick turns it into ``rollover_failed``.

    ``retry_failed`` is the bootstrap's re-trigger semantic (a failed task
    is reset for another attempt); a rollover passes False, because new
    code is not a retry request.
    """
    discovery = await discover_and_register_aio(
        registry,
        build_id,
        tuple(roots),
        retry_failed=retry_failed,
        limit_key_selector=limit_key_selector,
        scope_key=scope_key,
    )
    # --- rehydration pre-flight (see _preflight_rehydration): raises ---
    _preflight_rehydration(
        build_id, discovery.incomplete.values(), task_module_patterns
    )
    await registry.build_set_scope_aio(
        build_id, scope_key=scope_key, build_config=build_config
    )
    return discovery


def _run_reactive_bootstrap_scoped(
    build_id: UUID,
    task_list: list[BaseTask],
    *,
    registry: typing.Any,
    app_name: str,
    tick_kwargs: dict[str, typing.Any] | None,
    task_module_patterns: typing.Sequence[str],
    limit_key_selector: LimitKeySelector | None,
    build_config: typing.Mapping[str, typing.Mapping[str, typing.Any]] | None,
) -> ReactiveBootstrapResult:
    """:func:`run_reactive_bootstrap` with the build config already installed."""
    # The scope hash validates every class the build config names, and a
    # configured class need not be one the roots import — a dynamic
    # upstream three yields down is the typical case. Register the app's
    # declared task modules first, exactly as the deployed ``build`` and
    # ``tick`` wrappers do; cached per module list, so a warm container
    # pays nothing.
    if task_module_patterns:
        import_task_modules(expand_task_module_patterns(task_module_patterns))
    scope_key = structure_scope_key(code_id(), build_config)
    if build_config:
        # Only with a config to resolve: the roots arrived by value from the
        # trigger, constructed before any config existed, so their
        # non-identity fields are the defaults. Re-creating them here is
        # what makes the config reach them. Without a config there is
        # nothing to resolve and the objects stay exactly as sent.
        task_list = [rebind_to_build_config(task) for task in task_list]
    discovery = asyncio.run(
        plan_under_scope_aio(
            registry,
            build_id,
            task_list,
            scope_key=scope_key,
            build_config=build_config,
            task_module_patterns=task_module_patterns,
            limit_key_selector=limit_key_selector,
            retry_failed=True,
        )
    )
    # The reactive marker/owner/config, written LAST — see the ordering
    # guarantee in this function's docstring. This is an upsert: because
    # the registry is mutable — unlike a possibly immutable target root —
    # a re-trigger MAY update tick_kwargs. tick_kwargs is passed through
    # as-is: None (a bare re-trigger) preserves the stored config
    # server-side rather than wiping it.
    registry.build_set_reactive_meta(
        build_id, app_name=app_name, tick_kwargs=tick_kwargs
    )
    tick_function = modal.Function.from_name(app_name=app_name, name="tick")
    tick_call = tick_function.spawn(build_id=str(build_id))
    summary = {
        "build_id": str(build_id),
        "scope_key": scope_key,
        "roots": len(task_list),
        "incomplete": len(discovery.incomplete),
        "previously_completed": len(discovery.previously_completed),
        "retried": len(discovery.retried),
    }
    logger.info(f"Reactive bootstrap for build {build_id}: {summary}")
    return ReactiveBootstrapResult(summary=summary, tick_call=tick_call)
