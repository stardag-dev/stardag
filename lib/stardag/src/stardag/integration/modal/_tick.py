"""Reactive scheduling: the tick body, its configuration, and the watchdog.

A *tick* is one short-lived pass over a reactive build's frontier. Ticks are
spawned by the bootstrap, by workers finishing tasks, and by the optional
watchdog; they are idempotent and single-flighted, so invoking one at any time
is safe and a tick on a non-reactive build no-ops.

The tick is deployed as a Modal function, so it runs in a container with no
access to the ``StardagApp`` object that configured it. Everything it needs
from deploy time is therefore captured in a :class:`_TickDeployment` at
``finalize()`` and closed over by the registered wrapper — which keeps that
wrapper a two-line delegation to :func:`_run_tick` here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import typing
from uuid import UUID

import modal

from stardag import BaseTask
from stardag.build import (
    BuildTaskStore,
    FailMode,
    InterruptionPolicy,
    TickConfig,
    run_tick_aio,
)
from stardag.build._base import GlobalLockConfig
from stardag.build._task_modules import (
    import_task_modules,
    set_declared_task_module_patterns,
)
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._selector import WorkerSelector
from stardag.integration.modal._settings import FunctionSettings
from stardag.registry._base import NoOpRegistry, registry_provider
from stardag.registry._lock import RegistryGlobalConcurrencyLockManager

logger = logging.getLogger(__name__)

LimitKeySelector = typing.Callable[[BaseTask], typing.Sequence[str]]
"""Maps a task to the named registry concurrency-limit keys it runs under."""

InterruptionPolicySelector = typing.Callable[[BaseTask], InterruptionPolicy]
"""Maps a task to what a platform interruption of it should mean.

``RESTART`` (the default for every task when no selector is configured)
resumes a task the platform interrupted; ``FAIL`` is the opt-in for one
where hitting the timeout means it hung. See :class:`InterruptionPolicy`.
"""


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
    # Per-build attempt budget. Belongs here more than most: the budget is
    # per build by definition, and a build that has exhausted it is
    # unblocked by re-triggering *with a raised* max_attempts — which only
    # works if a re-trigger can say it.
    "max_attempts",
    # ...and the same argument for the interruption budget beside it: a
    # long training run that gets killed and resumed more often than
    # expected is recovered by re-triggering with a raised value, which
    # only works if a re-trigger can say it.
    "max_interruptions",
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


def _build_tick_config(
    stored_tick_kwargs: dict[str, typing.Any] | None,
    tick_kwargs: dict[str, typing.Any] | None,
    limit_key_selector: LimitKeySelector | None,
    tick_timeout_seconds: float | None = None,
    interruption_policy_selector: "InterruptionPolicySelector | None" = None,
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
    than an override so a caller that runs several ticks in one container
    can pass its own share of the budget (the watchdog sweep does), and it
    is deliberately absent from ``_TICK_KWARGS_ALLOWED``: persisting it in
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
        interruption_policy_selector=interruption_policy_selector,
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
        interruption_policy_selector: What a platform interruption of a
            given task should mean (resume it, or fail it). Same
            deployed-app-configuration argument as the selector above.
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
        task_module_patterns: The declared patterns behind that list,
            published for the coverage checks that report against patterns
            rather than expansions.
    """

    app_name: str
    worker_selector: WorkerSelector
    limit_key_selector: LimitKeySelector | None
    interruption_policy_selector: InterruptionPolicySelector | None
    modal_workspace: str | None
    worker_timeouts: dict[str, int]
    tick_timeout_seconds: float | None
    task_modules: tuple[str, ...]
    task_module_patterns: tuple[str, ...]


def _run_tick(
    build_id: str,
    tick_kwargs: dict[str, typing.Any] | None = None,
    *,
    deployment: _TickDeployment,
) -> dict[str, typing.Any]:
    """One scheduler pass over a reactive build's frontier.

    The body of the deployed ``tick`` function (see
    :meth:`StardagApp.finalize`), and also what the watchdog sweep invokes
    in-process for each build it adopts. Returns a JSON-able outcome: the
    ``run_tick_aio`` summary, or a short ``{"outcome": ...}`` record for
    the two cases that stop before the scheduler lease is even acquired —
    a build that is not reactively scheduled, and one owned by another app.
    """
    _setup_logging()
    app_name = deployment.app_name
    build_uuid = UUID(build_id)
    task_store = BuildTaskStore(build_uuid)
    registry = registry_provider.get()
    # The reactive marker/owner/config live in the registry (not on
    # the target root): read them with the lighter GET /builds/{id}
    # for this pre-lease gate — the full frontier is only fetched
    # once the tick actually processes it (run_tick_aio). Reactive
    # scheduling against a server predating the build_get shape 404s
    # only on a genuine missing route; a resource-level 404 (build
    # deleted) must propagate as a real not-found.
    try:
        build_info = registry.build_get(build_uuid)
    except NotFoundError as e:
        if not is_missing_route_error(e):
            raise
        raise RuntimeError(
            "The registry server does not support reactive "
            "scheduling (build endpoint too old). Upgrade "
            "stardag-api to a version matching this SDK."
        ) from e
    reactive_app_name = build_info.reactive_app_name
    if reactive_app_name is None:
        # Not a reactively-scheduled build (e.g. a resident-
        # orchestrator build swept by the watchdog): never schedule
        # on top of it, and don't even acquire the scheduler lease.
        logger.info(
            f"Tick for build {build_id}: not reactively scheduled "
            "(no reactive_app_name); skipping."
        )
        return {"outcome": "not_reactive"}
    # App ownership: with multiple StardagApps in one environment,
    # every app's watchdog sweeps ALL running reactive builds — but
    # only the app recorded at trigger time may drive a build.
    # A foreign app's tick would schedule with ITS commit (its
    # workers, its selectors) and unpickle the owning app's task
    # store (pickle skew across commits), so it must not run the
    # tick loop itself. Instead it FORWARDS: best-effort spawn of
    # the owner's tick, so wake-ups that land on the wrong app
    # (e.g. a still-running worker of the previous owner after a
    # takeover) are not dropped, and every app's watchdog sweep
    # doubles as cross-app coverage. The owner-side single-flight
    # lease collapses duplicate forwards. Explicit takeover =
    # re-trigger from the new app (updates reactive_app_name and
    # re-persists the task objects under the new code).
    owner_app = reactive_app_name
    if owner_app != app_name:
        forwarded = False
        try:
            modal.Function.from_name(app_name=owner_app, name="tick").spawn(
                build_id=build_id
            )
            forwarded = True
        except Exception as e:
            # Owner app deleted/renamed: the build is orphaned —
            # surfaced in logs; remedy is a re-trigger from a live
            # app (see the how-to's app-ownership section).
            logger.info(
                f"Tick for build {build_id}: could not forward to "
                f"owner app {owner_app!r} (deleted?): {e}"
            )
        logger.info(
            f"Tick for build {build_id}: owned by app "
            f"{owner_app!r}, not {app_name!r}; "
            f"{'forwarded to owner' if forwarded else 'skipping'}."
        )
        return {
            "outcome": "foreign_app",
            "owner_app": owner_app,
            "forwarded": forwarded,
        }
    # Per-build tick configuration persisted at trigger time in the
    # registry — every tick (worker wake-ups and watchdog sweeps
    # spawn with only the build id) runs with the same settings.
    # Explicit tick_kwargs (tests/manual invocations) win over
    # persisted ones; the limit key selector is deployed-app config.
    config = _build_tick_config(
        build_info.reactive_tick_kwargs,
        tick_kwargs,
        deployment.limit_key_selector,
        interruption_policy_selector=deployment.interruption_policy_selector,
        tick_timeout_seconds=deployment.tick_timeout_seconds,
    )

    # Register the app's task classes in THIS container before the
    # tick reconstructs anything: rehydrating a task from registry
    # data is a dict lookup in the polymorphic registry, which is
    # populated only as a side effect of importing the defining
    # modules (unlike pickle, which self-imports). The list was
    # expanded and frozen at deploy time; the import is cached per
    # module list, so a container serving many ticks pays it once.
    # Failures warn rather than abort — and are retained, so a
    # later "could not rehydrate" error can name them.
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
    lock_manager = RegistryGlobalConcurrencyLockManager(
        # No waiting on the scheduler lease: a held lease means
        # another tick is active and will observe the wake-up flag.
        config=GlobalLockConfig(lock_wait_timeout_seconds=None),
    )
    summary = asyncio.run(
        run_tick_aio(
            build_uuid,
            registry=registry,
            task_executor=executor,
            lock_manager=lock_manager,
            task_store=task_store,
            config=config,
        )
    )
    logger.info(f"Tick for build {build_id}: {summary}")
    return dataclasses.asdict(summary)


# --- The watchdog sweep ---


def _run_watchdog_sweep(
    registry: typing.Any,
    tick: typing.Callable[..., typing.Any],
    sweep_limit: int = 100,
    tick_timeout_seconds: float | None = None,
    reactive_app_name: str | None = None,
) -> None:
    """One watchdog pass: tick every running build this app owns.

    ``reactive_app_name`` scopes the listing to this app's own reactive
    builds. Without it, ``sweep_limit`` is spent on whatever RUNNING builds
    happen to be most recently active in the environment — including builds
    no tick of this app can advance (resident builds, and builds whose
    orchestrator died without emitting a terminal event, which stay RUNNING
    forever). Once those exceed the limit the safety net stops reaching
    genuine reactive builds entirely, and silently.

    The trade-off is losing the incidental cross-app coverage a sweep used to
    provide. That was accidental and competed for the same limit; an app's own
    watchdog is the supported mechanism, and ``build_trigger`` already warns
    when a reactive build is triggered on an app without one.

    ``linger_seconds=0`` (one frontier pass per build) is essential: the
    sweep runs ticks sequentially in one function call — persisted linger
    settings (default 120 s) would blow through the function timeout after
    a couple of builds and starve the rest of the safety-net tick.

    The same "one container, many builds" property applies to the per-pass
    spawn cap, which is derived from how long the container may live: every
    build in the sweep would otherwise size its fan-out as though it had
    the whole container to itself, and the first wide build would spend the
    entire timeout while the rest of the sweep never ran. Each build is
    therefore handed its **share** of the budget. Truncating here is
    cheap and self-correcting — the watchdog is a safety net, builds are
    normally driven by their own ticks, and a truncated pass re-acts on a
    fresh frontier immediately.
    """
    if type(registry) is NoOpRegistry:
        logger.warning("Tick watchdog: no registry configured; nothing to do.")
        return
    # Scoping is server-side now that `build_list_running` is expressed in
    # terms of `build_list`, so every RegistryABC gets it and the signature
    # shim this used to need went with it. `scoped` still exists because
    # the truncation remedy below differs by it.
    scoped = reactive_app_name is not None
    running_builds = registry.build_list_running(
        limit=sweep_limit, reactive_app_name=reactive_app_name
    )
    scope = (
        f"reactive builds owned by {reactive_app_name!r}"
        if scoped
        else "running builds"
    )
    if len(running_builds) >= sweep_limit:
        # The remedy follows `scoped`, not whether a name was *asked* for:
        # a scoping request the registry cannot honour still yields a
        # listing capped by RUNNING builds of every kind, and "run fewer
        # reactive builds per app" would then be advice about the wrong
        # population.
        remedy = (
            "Cancel or clean up builds that are RUNNING but abandoned, or "
            "reduce the number of concurrent reactive builds for this app."
            if scoped
            else "This listing was not scoped to a reactive app, so the cap "
            "was consumed by RUNNING builds of every kind — most likely "
            "abandoned ones. Clean those up (`stardag builds cleanup`); an "
            "upgraded registry would also scope this listing."
        )
        logger.warning(
            f"Tick watchdog: {sweep_limit}+ {scope}; only the {sweep_limit} "
            "most recently active are swept, so a less-recently-active build "
            f"may not be ticked this period. {remedy}"
        )
    sweep_kwargs: dict[str, typing.Any] = {"linger_seconds": 0}
    if tick_timeout_seconds is not None and running_builds:
        sweep_kwargs["tick_timeout_seconds"] = tick_timeout_seconds / len(
            running_builds
        )
    for running_build_id in running_builds:
        try:
            tick(str(running_build_id), tick_kwargs=dict(sweep_kwargs))
        except Exception:
            logger.exception(f"Watchdog tick failed for build {running_build_id}")
