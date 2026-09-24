"""Reactive scheduling: the tick body, its configuration, and the watchdog.

A *tick* is one short-lived pass over a reactive build's frontier. Ticks are
spawned by the bootstrap, by workers finishing tasks, and by the optional
watchdog; they are idempotent and single-flighted, so invoking one at any time
is safe and a tick on a non-reactive build no-ops.

The tick is deployed as a Modal function, so it runs in a container with no
access to the ``StardagApp`` object that configured it. Everything it needs
from deploy time is therefore captured in a :class:`_TickDeployment` at
``finalize()`` and closed over by the registered wrapper — which keeps that
wrapper a two-line delegation to :func:`_run_deployed_tick_aio` here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import typing
from uuid import UUID


from stardag.build import (
    FailMode,
    TickConfig,
    run_tick_aio,
)
from stardag.build._deployment import own_deployment_id
from stardag.build._reactive import roll_over_aio
from stardag.build._registration import Walk
from stardag.build._wakeups import SpawnTick
from stardag.build._task_modules import (
    import_task_modules,
    set_declared_task_module_patterns,
)
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._limit_keys import LimitKeySelector
from stardag.integration.modal._selector import WorkerSelector
from stardag.integration.modal._spawn import spawn_tick
from stardag.integration.modal._settings import FunctionSettings
from stardag.registry import BuildFrontier, is_noop_registry, registry_provider

logger = logging.getLogger(__name__)


# --- Per-build tick configuration ---


_TICK_KWARGS_ALLOWED = (
    "linger_seconds",
    "poll_interval_seconds",
    "fail_mode",
    # Fan-out throttles. Both default to something derived (see TickConfig
    # and stardag.build._reactive._spawn_cap) and both are here because the
    # thing the derivation cannot see — how the *tick* function is sized
    # relative to its workers, and how much concurrency the registry
    # deployment behind it will take — is per-deployment, and a build
    # triggered against that deployment is where it can be said.
    "max_concurrent_actions",
    "max_spawns_per_tick",
    # How many times a pass tries to spawn a claimed execution.
    "max_attempts",
    # Completion checks in flight while a discovery job walks: a property
    # of the target backend this build's tasks write to.
    "max_concurrent_discover",
)


def _tick_function_timeout_seconds(
    tick_settings: FunctionSettings | None,
    builder_settings: FunctionSettings | None,
) -> float | None:
    """The Modal ``timeout`` the deployed ``tick`` function will carry.

    Resolved from **whichever settings actually register the function** —
    ``tick_settings`` when given, otherwise ``builder_settings``, which is
    the same fallback ``finalize`` applies. Reading ``tick_settings`` alone
    would silently yield "unknown" for every app that does not configure
    the tick separately (the common case), and a spawn cap derived from a
    timeout the function was not registered with is precisely the mistake
    this plumbing exists to remove.

    ``None`` when neither declares one: Modal's own default is not a
    promise this SDK should encode, and the cap has further rungs to fall
    back to (see ``stardag.build._reactive._spawn_cap``).
    """
    # An empty `tick_settings` falls back deliberately — it declares
    # nothing, and `finalize` resolves it the same way.
    settings = tick_settings if tick_settings else builder_settings
    # `is not None`, not truthiness: `timeout=0` is a value someone
    # configured, and reporting it as "not declared" would hand the spawn
    # cap a different rung to fall back to than the one the function was
    # actually registered with.
    timeout = (settings or {}).get("timeout")
    return float(timeout) if timeout is not None else None


# The one way this integration starts a tick; see ``_spawn``.
_spawn_tick = spawn_tick


# What the watchdog asks each build's tick for: one pass, no linger.
#
# A wake-up means "something changed for this build", and the tick it spawns
# should linger — while a DAG churns, each action resets the deadline and one
# resident scheduler drives level after level without a cold start per level.
#
# A sweep means the opposite: nobody told us anything, we are looking in case
# something was missed. Its population is builds where, by construction,
# nothing is known to have happened — a lost wake-up, an abandoned RUNNING
# build, a laptop-only build, an event nobody wrote. Lingering there spends
# container time on the builds least likely to have anything to do, which is
# the wrong way round for a safety net.
#
# The sharper reason, and the one that will outlive per-tick containers: a
# container's lifetime is the **maximum** over its live inputs, not the sum.
# One lingering tick holds the whole container open, so a couple of stale
# RUNNING builds swept on a 5-minute period, each lingering the default
# 120 s, keep the tick function warm ~40 % of the time — for nothing, and
# regardless of how few they are. ``container_idle_timeout`` never gets a
# chance to fire. Packing ticks onto shared containers does not fix that; it
# makes one abandoned build everybody's problem.
#
# So the sweep keeps the ``linger_seconds=0`` the inline version used, but
# now for reasons of its own rather than to survive sharing one container.
_SWEEP_TICK_KWARGS = {"linger_seconds": 0}


def _spawn_sweep_tick(build_id: UUID, app_name: str) -> None:
    """Spawn a watchdog tick: one pass over this build, then exit."""
    _spawn_tick(build_id, app_name, tick_kwargs=dict(_SWEEP_TICK_KWARGS))


def _build_tick_config(
    stored_tick_kwargs: dict[str, typing.Any] | None,
    tick_kwargs: dict[str, typing.Any] | None,
    limit_key_selector: LimitKeySelector | None,
    tick_timeout_seconds: float | None = None,
    spawn_tick: SpawnTick | None = None,
) -> TickConfig:
    """Assemble a TickConfig for one tick invocation.

    Precedence: explicit ``tick_kwargs`` (manual/ops invocations) over the
    build's stored ``reactive_tick_kwargs`` (set at trigger time in the
    registry — shared by all ticks) over TickConfig defaults. The
    concurrency-limit key selector is deployed-app configuration (callables
    can't ride in the JSON tick config).

    ``tick_timeout_seconds`` is the deployed ``tick`` function's own Modal
    ``timeout`` — how long this container may live, which is what the
    per-pass spawn cap is derived from. It is applied as a *default* rather
    than an override so an explicit ``tick_kwargs`` (a test, or a manual
    invocation) still wins; the watchdog sweep used to be the caller that
    needed this, passing each build its share of one container's budget,
    and no longer runs ticks in-process at all. It is deliberately absent
    from ``_TICK_KWARGS_ALLOWED``: persisting it in
    a build's stored tick config would freeze a deploy-time fact into
    per-build state and go stale on the next redeploy.
    """
    config_kwargs: dict[str, typing.Any] = {
        **(stored_tick_kwargs or {}),
        **(tick_kwargs or {}),
    }
    if "fail_mode" in config_kwargs:
        config_kwargs["fail_mode"] = FailMode(config_kwargs["fail_mode"])
    config_kwargs.setdefault("tick_timeout_seconds", tick_timeout_seconds)
    return TickConfig(
        limit_key_selector=limit_key_selector,
        spawn_tick=spawn_tick,
        **config_kwargs,
    )


def _validate_tick_kwargs(
    tick_kwargs: dict[str, typing.Any] | None,
) -> dict[str, typing.Any] | None:
    """Validate + JSON-normalize reactive tick_kwargs.

    They are persisted in the build's ``reactive_tick_kwargs`` in the
    registry (JSON) so all ticks of the build share them — hence only
    JSON-scalar TickConfig fields are allowed here. ``fail_mode`` may be
    passed as a FailMode and is stored as its string value.
    """
    if not tick_kwargs:
        return tick_kwargs
    unknown = set(tick_kwargs) - set(_TICK_KWARGS_ALLOWED)
    if unknown:
        raise TypeError(
            f"Unsupported tick_kwargs {sorted(unknown)}; allowed (JSON-"
            f"persistable TickConfig fields): {list(_TICK_KWARGS_ALLOWED)}. "
            "Callables like a concurrency-limit key selector belong in the "
            "deployed app configuration, not per-trigger kwargs."
        )
    normalized = dict(tick_kwargs)
    if "fail_mode" in normalized:
        normalized["fail_mode"] = str(FailMode(normalized["fail_mode"]))
    return normalized


# --- The deployed tick ---


@dataclasses.dataclass(frozen=True)
class _TickDeployment:
    """The deploy-time facts a scheduler tick needs, captured at ``finalize()``.

    A tick runs in a deployed container with no access to the
    ``StardagApp`` that configured it, so every one of these has to be
    carried across the Modal boundary in the registered wrapper's closure.
    Bundling them keeps that closure a single variable and gives the facts
    one documented home instead of eight free ones.

    Attributes:
        app_name: The Modal app the tick belongs to — also the ownership
            check against a build's recorded ``reactive_app_name``.
        worker_selector: The app's deployed selector. Always the app's own:
            per-trigger overrides are rejected precisely because later ticks
            could not honour them.
        limit_key_selector: Named registry concurrency-limit keys per task.
            Deployed-app configuration because a callable cannot be
            persisted in the build's JSON tick config.
        modal_workspace: Explicit Modal workspace for executor metadata, or
            None to resolve it best-effort.
        worker_timeouts: Per-worker Modal ``timeout`` (seconds), which is
            what the execution claim's expiry is derived from. Only the
            deploy process can see the app's ``worker_settings``.
        tick_timeout_seconds: The ``tick`` function's own Modal ``timeout``
            — how long this container may live, from which the per-pass
            spawn cap is derived.
        task_modules: The concrete module list expanded at ``finalize()``,
            imported here so a tick can rebuild task objects from registry
            data. Empty when the app opted out.
        task_module_patterns: The patterns behind that list — declared, or
            inferred when the app declared none — published for the
            coverage checks that report against patterns rather than
            expansions, and for a rollover's re-plan.
    """

    app_name: str
    worker_selector: WorkerSelector
    limit_key_selector: LimitKeySelector | None
    modal_workspace: str | None
    worker_timeouts: dict[str, int]
    tick_timeout_seconds: float | None
    task_modules: tuple[str, ...]
    task_module_patterns: tuple[str, ...]


async def _run_deployed_tick_aio(
    build_id: str,
    tick_kwargs: dict[str, typing.Any] | None = None,
    *,
    deployment: _TickDeployment,
) -> dict[str, typing.Any]:
    """One scheduler tick of a reactive build: the body of the deployed
    ``tick`` function (see :meth:`StardagApp.finalize`).

    Returns a JSON-able outcome: the ``run_tick_aio`` summary, or a short
    ``{"outcome": ...}`` for the two cases that stop before the lease — a
    build that is not reactively scheduled, and one owned by another app.

    **A coroutine awaited by the deployed wrapper**: ticks share a container
    (``_TICK_CONCURRENCY``), and they are safe to share because they share
    its event loop and so the process-wide ``APIRegistry``'s async client.

    The tick compares the active plan's deployment with its own
    ``STARDAG_DEPLOYMENT_ID``; on a difference it rolls the build over (see
    :func:`stardag.build._reactive.roll_over_aio`) when its deployment is
    the app's current one, and exits ``superseded`` otherwise.
    """
    _setup_logging()
    app_name = deployment.app_name
    build_uuid = UUID(build_id)
    registry = registry_provider.get()
    build_info = await registry.build_get_aio(build_uuid)
    owner_app = build_info.reactive_app_name
    if owner_app is None:
        logger.info(
            f"Tick for build {build_id}: not reactively scheduled "
            "(no reactive_app_name); skipping."
        )
        return {"outcome": "not_reactive"}
    if owner_app != app_name:
        # Only the app recorded at trigger time drives a build: a foreign
        # app's tick would schedule with its own workers and selectors. It
        # forwards instead (best-effort), so a wake-up landing on the wrong
        # app is not dropped; the owner's lease collapses duplicates.
        forwarded = False
        try:
            await asyncio.to_thread(_spawn_tick, build_uuid, owner_app)
            forwarded = True
        except Exception as e:
            logger.info(
                f"Tick for build {build_id}: could not forward to owner app "
                f"{owner_app!r} (deleted?): {e}"
            )
        return {
            "outcome": "foreign_app",
            "owner_app": owner_app,
            "forwarded": forwarded,
        }

    config = _build_tick_config(
        build_info.reactive_tick_kwargs,
        tick_kwargs,
        deployment.limit_key_selector,
        tick_timeout_seconds=deployment.tick_timeout_seconds,
        spawn_tick=_spawn_tick,
    )
    # Register the app's task classes before anything is rehydrated (the
    # polymorphic registry fills only as the defining modules import). On
    # the loop on purpose: user modules may have main-thread-only import
    # side effects; the per-module-list cache makes later ticks free.
    if deployment.task_modules:
        set_declared_task_module_patterns(deployment.task_module_patterns)
        import_task_modules(deployment.task_modules)

    executor = ModalTaskExecutor(
        modal_app_name=app_name,
        worker_selector=deployment.worker_selector,
        reactive=True,
        modal_workspace=deployment.modal_workspace,
        worker_timeouts=deployment.worker_timeouts,
    )
    own = own_deployment_id()
    rollover_details: dict[str, typing.Any] = {}

    async def _roll_over(frontier: BuildFrontier):
        from stardag.integration.modal._bootstrap import _preflight_rehydration

        def preflight(walk: Walk) -> None:
            _preflight_rehydration(
                build_uuid, walk.incomplete, deployment.task_module_patterns
            )

        assert own is not None
        try:
            return await roll_over_aio(
                registry,
                frontier,
                own_deployment_id=own,
                preflight=preflight,
                max_concurrent_discover=config.max_concurrent_discover,
            )
        except Exception as e:
            rollover_details.update(getattr(e, "payload", {}) or {})
            raise

    summary = await run_tick_aio(
        build_uuid,
        registry=registry,
        task_executor=executor,
        config=config,
        deployment_id=own,
        roll_over=_roll_over if own is not None else None,
    )
    # The container id is in the line because ticks share containers.
    logger.info(
        f"Tick for build {build_id} (container "
        f"{os.environ.get('MODAL_TASK_ID', 'unknown')}): {summary}"
    )
    result = dataclasses.asdict(summary)
    if summary.outcome == "rollover_failed":
        result.update(rollover_details)
    return result


# --- The watchdog sweep ---


def _run_watchdog_sweep(
    registry: typing.Any,
    reactive_app_name: str,
    sweep_limit: int = 100,
    spawn: SpawnTick | None = None,
) -> None:
    """One watchdog pass: spawn a tick for every running build this app owns.

    The sweep *dispatches*; it does not schedule. It lists the builds, spawns
    one ``tick`` each on this app, and returns — in seconds, however many
    builds there are. Each build then gets its own container and its own full
    timeout, rather than a share of the sweep's.

    What it does **not** get is a linger: the sweep asks for one pass (see
    ``_SWEEP_TICK_KWARGS``). A wake-up's tick lingers because something
    happened and more is likely to; a sweep's should not, because its
    population is builds where nothing is known to have happened at all.

    A spawn that duplicates a tick already running is not free — a container
    starts either way — but it is cheap and self-limiting: the second tick
    finds the scheduler lease held and exits without acting.

    It used to run the tick body for every build sequentially inside the
    *sweep's* single container, which made three things a function of how
    many builds the environment happened to be running: each build's spawn
    cap (that one container's timeout was divided across the sweep), the
    latency for the last build in the list (it waited behind all the
    others), and whether the sweep finished at all. Dispatching removes all
    three, and with them the share-of-timeout override.

    ``reactive_app_name`` scopes the listing to this app's own reactive
    builds, and is now also *where each tick is spawned*. Without scoping,
    ``sweep_limit`` would be spent on whatever RUNNING builds happen to be
    most recently active in the environment — including builds no tick of
    this app can advance (resident builds, and builds whose orchestrator died
    without emitting a terminal event, which stay RUNNING forever). Once
    those exceed the limit the safety net stops reaching genuine reactive
    builds entirely, and silently.

    The trade-off is losing the incidental cross-app coverage a sweep used to
    provide. That was accidental and competed for the same limit; an app's own
    watchdog is the supported mechanism, and ``build_trigger`` already warns
    when a reactive build is triggered on an app without one.

    Scoping is server-side, so a server predating the filter ignores it and
    answers with RUNNING builds of every kind. That degraded case got more
    expensive with dispatch, not less: a build this app cannot advance used
    to cost an in-process no-op and now costs a container start that exits
    on ``not_reactive`` or ``foreign_app``. It is bounded by ``sweep_limit``
    and by the watchdog period, and the remedy is the same as it was —
    upgrade the registry, or clean up abandoned RUNNING builds.
    """
    if is_noop_registry(registry):
        logger.warning("Tick watchdog: no registry configured; nothing to do.")
        return
    spawn = spawn or _spawn_sweep_tick
    running_builds = registry.build_list_running(
        reactive_app_name=reactive_app_name, limit=sweep_limit
    )
    if len(running_builds) >= sweep_limit:
        logger.warning(
            f"Tick watchdog: {sweep_limit}+ reactive builds owned by "
            f"{reactive_app_name!r}; only the {sweep_limit} most recently "
            "active are swept, so a less-recently-active build may not be "
            "ticked this period. Cancel or clean up builds that are RUNNING "
            "but abandoned, or reduce the number of concurrent reactive "
            "builds for this app."
        )
    spawned = 0
    for running_build_id in running_builds:
        try:
            spawn(running_build_id, reactive_app_name)
        except Exception:
            # One unspawnable build must not cost the rest of the sweep.
            logger.exception(
                f"Watchdog could not spawn a tick for build {running_build_id}"
            )
            continue
        spawned += 1
    logger.info(
        f"Tick watchdog: spawned {spawned} tick(s) for the "
        f"{len(running_builds)} running build(s) owned by "
        f"{reactive_app_name!r}."
    )
