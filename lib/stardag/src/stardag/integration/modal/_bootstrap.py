"""Reactive bootstrap: everything a reactive build needs before it can tick.

:func:`run_reactive_bootstrap` discovers the DAG, checks task-module coverage,
persists the task store, arms the build and spawns the first tick. It normally
runs **inside Modal**, as the body of the deployed ``bootstrap`` function
(discovery is target-root I/O, which is far cheaper next to a mounted volume
than from a laptop), and runs in the triggering process instead when an app
opts out with ``StardagApp(reactive_discovery="local")``.

Also here: the coverage checks and the pickle-elision decision the bootstrap
applies, and :func:`_fail_build_best_effort`, the "never leave an orphan
RUNNING build" helper its callers share.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from uuid import UUID

import modal

from stardag import BaseTask
from stardag.build import BuildTaskStore, discover_and_register_aio
from stardag.integration.modal._limit_keys import LimitKeySelector
from stardag.build._task_modules import (
    PickleElisionPlan,
    TaskModulesError,
    format_uncovered_message,
    plan_pickle_elision,
    uncovered_task_classes,
)

logger = logging.getLogger(__name__)


def _preflight_task_modules(
    tasks: typing.Iterable[BaseTask], task_module_patterns: typing.Sequence[str]
) -> None:
    """Warn about discovered task classes the declared patterns don't cover.

    Takes the app's declared ``task_modules`` **patterns**, not the module
    list they expand to: coverage is a question about patterns, and
    :func:`uncovered_task_classes` matches classes against them. (The
    expansion is a separate deploy-time artifact — it is what a tick
    imports; see :class:`stardag.integration.modal._tick._TickDeployment`.)

    **The authoritative coverage check.** It runs wherever discovery runs
    — normally the bootstrap container — on the set discovery just walked:
    those are exactly the tasks a tick may have to rehydrate, and reusing
    discovery's (pruned) walk avoids a second traversal of the DAG. It is
    never skipped, and it is what gates ``require_pickle_free`` (via the
    persistence step, which additionally knows about round-trip failures,
    not just coverage).

    **Severity is a warning, not an error** (unless
    ``require_pickle_free``). An uncovered class is not broken: it falls
    back to the pickle path, which is exactly how every reactive build
    worked before ``task_modules`` existed. Failing would therefore break
    working setups the moment they upgrade, which is precisely what "this
    feature is additive" forbids.

    In the default (bootstrap) placement these are the patterns **baked
    into the deployment at ``finalize()``** — not the caller's local app
    definition. That closes the stale-deploy blind spot this check used to
    carry: it compares the DAG against the patterns the deployed ticks
    were built from, so "you changed ``task_modules`` but didn't redeploy"
    is visible rather than silently agreeable.

    Skipped entirely when the app opted out of ``task_modules`` — an app
    that never declared any would otherwise warn about every class in
    every DAG, on every trigger.
    """
    if not task_module_patterns:
        return
    uncovered = uncovered_task_classes(tasks, task_module_patterns)
    if not uncovered:
        return
    logger.warning(
        format_uncovered_message(
            uncovered,
            task_module_patterns,
            remedy=(
                "Until then these tasks stay dependent on their "
                "build-task-store pickles, which need target-root "
                "write access and are invalidated by a redeploy."
            ),
        )
    )


def _advise_uncovered_root_task_modules(
    root_tasks: typing.Sequence[BaseTask], task_module_patterns: typing.Sequence[str]
) -> None:
    """Advisory, roots-only coverage note emitted at the trigger.

    Takes the declared ``task_modules`` **patterns**, like
    :func:`_preflight_task_modules`.

    Purely additive early feedback, and deliberately **not** a check in
    its own right: :func:`_preflight_task_modules` is the authoritative
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
                "discovery runs and may name more classes."
            ),
        )
    )


def _persist_discovered_tasks(
    build_id: UUID,
    tasks: typing.Iterable[BaseTask],
    *,
    task_module_patterns: typing.Sequence[str],
    elide_pickles: bool,
    require_pickle_free: bool,
) -> None:
    """Write the build task store, skipping pickles that aren't needed.

    Takes the declared ``task_modules`` **patterns** —
    :func:`plan_pickle_elision` matches task classes against them, exactly
    as the coverage check does.

    Unless the app *opted in* (``elide_pickles``), this is byte-for-byte
    the old behaviour: pickle everything. With opt-in, each task gets a
    dry run of what a tick will do — reconstruct it from exactly the
    payload registration stored — and only the ones that fail keep a
    pickle. A build whose classes are all covered writes nothing to the
    target root at all.

    Runs **inside the bootstrap container**, which is a large part of why
    the move is worth doing: for a ``modalvol://`` target root the store
    is a mounted filesystem here and a rate-limited volume API from a
    laptop. The same writes that used to be N remote calls from the
    trigger are now N local ones.

    ``elide_pickles`` is the app's opt-in — ``task_modules`` passed
    explicitly (or ``require_pickle_free``), NOT merely inferred —
    resolved at ``finalize()`` and baked in alongside the patterns.
    Inference must stay observation-only: it happens for every app,
    including apps written before the feature existed, and an SDK upgrade
    must never start dropping pickles on its own.
    """
    # Deliberately NOT a ``pickle_free=require_pickle_free`` store, unlike
    # the tick's (see ``_TickDeployment.require_pickle_free``). Here the
    # gate below is the enforcement, and it is exact: it names the offending
    # tasks and fails the build. A store that silently dropped the same
    # writes would, if the gate ever stopped firing first, leave a build
    # that starts fine and stalls later at "could not rehydrate" — strictly
    # the worse failure. The tick has no such gate and nothing to lose: its
    # write-back is a cache over an object it already holds.
    store = BuildTaskStore(build_id)
    if not task_module_patterns or not elide_pickles:
        store.save_tasks(tasks)
        return
    plan: PickleElisionPlan = plan_pickle_elision(tasks, task_module_patterns)
    if require_pickle_free:
        error = plan.require_pickle_free_error()
        if error is not None:
            raise TaskModulesError(error)
    store.save_tasks(task for task, _ in plan.pickled)
    logger.info(f"Build {build_id} task store: {plan.summary()}")


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
    elide_pickles: bool,
    require_pickle_free: bool,
    limit_key_selector: LimitKeySelector | None = None,
) -> ReactiveBootstrapResult:
    """Discover the DAG, persist it, arm the build, spawn the first tick.

    Everything a reactive build needs before it can be scheduled, except
    minting the build and registering its roots — those cost no target
    I/O and must happen at the trigger, before anything is spawned.

    Normally this runs **inside Modal**, as the body of the deployed
    ``bootstrap`` function, because discovery is target-root I/O:
    ``complete_aio()`` is one target existence check per task, and for a
    ``modalvol://`` root that is a rate-limited Volume *API* call from
    outside Modal versus a ``stat`` on a mounted filesystem inside it.
    The task-store writes below move with it for the same reason. The
    same code also runs at the trigger when an app opts out with
    ``StardagApp(reactive_discovery="local")``.

    **The ordering guarantee — do not "tidy" this.** The reactive marker
    (``build_set_reactive_meta``, which is what makes ``reactive_app_name``
    non-None) is written **last**, after discovery *and* persistence have
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
    discovery = asyncio.run(
        discover_and_register_aio(
            registry,
            build_id,
            tuple(task_list),
            retry_failed=True,
            limit_key_selector=limit_key_selector,
        )
    )
    # --- task-module coverage pre-flight (see _preflight_task_modules) ---
    _preflight_task_modules(discovery.incomplete.values(), task_module_patterns)
    # --- task persistence, with conditional pickle elision ---
    _persist_discovered_tasks(
        build_id,
        discovery.incomplete.values(),
        task_module_patterns=task_module_patterns,
        elide_pickles=elide_pickles,
        require_pickle_free=require_pickle_free,
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
        "roots": len(task_list),
        "incomplete": len(discovery.incomplete),
        "previously_completed": len(discovery.previously_completed),
        "retried": len(discovery.retried),
    }
    logger.info(f"Reactive bootstrap for build {build_id}: {summary}")
    return ReactiveBootstrapResult(summary=summary, tick_call=tick_call)
