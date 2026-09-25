"""The functions a stardag deployment consists of, registered at
``StardagApp.finalize()``: ``build``, one ``worker_<name>`` per worker,
``tick``, ``bootstrap`` and ``tick_watchdog``.

The wrappers are thin closures over what ``finalize`` resolved at deploy
time, because a deployed container never sees the ``StardagApp`` object.
Every wrapper opens with the app's ``container_setup``.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import typing
from uuid import UUID

import modal
from modal.exception import NotFoundError as ModalNotFoundError

from stardag import BaseTask
from stardag.build import BuildSummary
from stardag.build._task_modules import (
    import_task_modules,
    set_declared_task_module_patterns,
)
from stardag.integration.modal._bootstrap import (
    _fail_build_best_effort,
    run_reactive_bootstrap,
)
from stardag.integration.modal._container_setup import _run_container_setup
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._protocols import (
    _callable_accepts_env_overrides,
    _RunFunctionWithEnv,
)
from stardag.integration.modal._selector import WorkerSelector
from stardag.integration.modal._settings import (
    FunctionSettings,
    InputConcurrency,
    _prepare_function_settings,
)
from stardag.integration.modal._tick import (
    _run_deployed_tick_aio,
    _run_watchdog_sweep,
    _tick_function_timeout_seconds,
    _TickDeployment,
)
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.build._settings import settings_owner
from stardag.exceptions import StardagError
from stardag.integration.modal._metadata import (
    STARDAG_BUILD_ID_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    _get_modal_workspace,
)
from stardag.integration.modal._target import get_default_volume_mount_path
from stardag.integration.modal._volumes import TargetRootsVolumes
from stardag.registry import registry_provider
from stardag.utils.env import temp_env_vars

if typing.TYPE_CHECKING:
    from stardag.integration.modal._app import StardagApp

logger = logging.getLogger(__name__)

# How many scheduler ticks one container serves at once: **one**, and the
# same for the bootstrap and every worker function
# (``one_build_per_process`` below).
#
# **Why.** A tick and a worker apply their build's ``settings`` as
# process-global environment variables (D4), across the whole pass or run;
# a tick awaits inside that block. Two inputs sharing a container can
# therefore interleave, and one build's ``requires()``, selectors or task
# code would then read another build's values -- or lose its own to the
# other's restore. So a process applying settings serves one build at a
# time: deployed ticks and workers run one input per container and scale by
# containers. The cost is more containers (a lingering tick holds one of
# its own), accepted; ``settings_applied`` refuses an interleaving at run
# time as the backstop (design.md, "The deterministic scope"; decisions.md,
# implementation notes I7).
#
# The tick was packed ten to a container before settings existed, which is
# why it still passes a default here rather than none: the value is the
# decision, stated where the next reader looks for it.
_TICK_CONCURRENCY: InputConcurrency = {"max_inputs": 1}


def _refuse_packed_settings_function(
    name: str, concurrency: InputConcurrency | None
) -> None:
    """Refuse input concurrency above one on a function that applies a
    build's settings (the tick, the bootstrap and every worker): the deploy
    fails here, in the setting name the app wrote, instead of the first two
    overlapping builds failing at run time."""
    if concurrency is None or concurrency.get("max_inputs", 1) <= 1:
        return
    raise StardagError(
        f"FunctionSettings for {name!r} sets max_concurrent_inputs="
        f"{concurrency['max_inputs']}. {name!r} applies its build's settings "
        "as process-wide environment variables, so a container serves one "
        "build at a time: leave max_concurrent_inputs unset (or 1) and scale "
        "with containers (max_containers)."
    )


def _infer_task_module_patterns(_depth: int = 2) -> tuple[str, ...]:
    """Infer ``task_modules`` from the module that constructs the app.

    The default declaration is "the root package of the module defining
    this app, recursively" — which is right far more often than not: an
    app and the tasks it schedules almost always live in the same
    distribution, and a whole-package wildcard costs only import time.

    Inference is impossible for a module that is not part of a package —
    ``__main__``, or a loose script that Modal loads as a top-level module.
    Such a module isn't importable in a container under a stable name in
    the first place, so a pattern derived from it would be a lie. We warn
    and opt out, rather than baking in a module list that would fail to
    import in every tick container. An app that opts out (here or with
    ``task_modules=[]``) is resident-only: a reactive trigger on it is
    refused, because a scheduler tick would have no way to rebuild a
    single one of its tasks.

    Args:
        _depth: Stack frames back to the user's call site (``__init__``'s
            caller by default). Not part of the public contract.
    """
    # Frames hold their locals and globals alive and participate in
    # reference cycles, so the walk is scoped and the references dropped
    # rather than left for the collector — this runs in long-lived
    # scheduler containers.
    frame = inspect.currentframe()
    try:
        for _ in range(_depth):
            frame = frame.f_back if frame is not None else None
        module_name = frame.f_globals.get("__name__") if frame is not None else None
        package = frame.f_globals.get("__package__") if frame is not None else None
    finally:
        del frame
    if not module_name or module_name == "__main__" or not package:
        logger.warning(
            "Could not infer StardagApp(task_modules=...): the app is "
            f"defined in {module_name or 'an unknown module'!r}, which is "
            "not part of an importable package. This app can only run "
            "RESIDENT builds — build_trigger(reactive=True) will be "
            "refused, because a scheduler tick rebuilds every task it "
            "schedules from registry data and can only do that for a "
            "class whose module it has imported. Declare the modules "
            'explicitly — e.g. task_modules=["my_pkg.tasks.*"] — or pass '
            "task_modules=[] to silence this warning."
        )
        return ()
    return (f"{module_name.split('.')[0]}.*",)


def _auto_mounted_volumes(
    target_roots_volumes: TargetRootsVolumes,
) -> tuple[dict[str, str], dict[str, modal.Volume]]:
    """Auto-mount mapping for the target roots' Modal volumes.

    Returns ``(volume_mounts, auto_volumes)``: ``mount_path ->
    volume_name`` for the env var that tells targets to use local I/O,
    and ``mount_path -> Volume`` for the Modal function settings.
    """
    volume_mounts: dict[str, str] = {}
    auto_volumes: dict[str, modal.Volume] = {}
    for vol_name, vol in target_roots_volumes.by_volume_name.items():
        mount_path = str(get_default_volume_mount_path(vol_name))
        volume_mounts[mount_path] = vol_name
        auto_volumes[mount_path] = vol
    return volume_mounts, auto_volumes


def _validate_api_key_secret(self: "StardagApp") -> None:
    """Fail deploy with a clear error if the named API-key secret is absent.

    Best-effort: only a definitive not-found errors out; no Modal
    context / auth just skips the check so offline finalize and unit
    tests aren't broken.
    """
    assert self.stardag_api_key_secret is not None
    try:
        self.stardag_api_key_secret.hydrate()
    except ModalNotFoundError as e:
        name = self._api_key_secret_name
        secret_name_flag = "" if name == "stardag-api-key" else f" --secret-name {name}"
        raise StardagError(
            f"StardagApp.stardag_api_key_secret refers to a Modal "
            f"secret named {name!r} that does not exist in the "
            f"current Modal environment. Run "
            f"`stardag modal stardag-api-key create"
            f"{secret_name_flag}` to mint a Stardag API key and "
            f"sync it into a Modal secret of that name, so the "
            f"deployed functions can authenticate to the "
            f"registry. If you supply the API key another way, or "
            f"set it per function, pass stardag_api_key_secret="
            f"None."
        ) from e
    except Exception as e:  # noqa: BLE001 - best-effort validation
        logger.debug(
            f"Could not validate stardag_api_key_secret "
            f"{self._api_key_secret_name!r} (no Modal context?); "
            f"proceeding: {e}"
        )


def _resolve_extra_secrets(
    self: "StardagApp",
    extra_secrets: list[modal.Secret] | None,
    volume_mounts: dict[str, str],
) -> list[modal.Secret]:
    """The secrets injected into *every* function this app registers.

    Order matters and is preserved: the caller's own secrets first,
    then the deploy-resolved ones. Later secrets win on conflicting
    env vars in Modal, and the earliest occurrence wins the name-based
    de-duplication in ``_prepare_function_settings``.
    """
    extra_secrets = list(extra_secrets or [])

    # Inject volume mount config as env var so ModalMountedVolumeFileTarget
    # is used
    if volume_mounts:
        extra_secrets.append(
            modal.Secret.from_dict(
                {"STARDAG_MODAL_VOLUME_MOUNTS": json.dumps(volume_mounts)}
            )
        )
    # Bake the Modal workspace into every function's env. It's needed for
    # the UI's Modal dashboard deep links (executor metadata), but the
    # only way to resolve it — the Modal token — exists in this deploy
    # process, NOT in the deployed containers. Resolve it here (or use
    # the explicit override) and propagate it so containers don't have to
    # (and can't) look it up. Best-effort: if it can't be resolved, deep
    # links degrade gracefully (the UI shows env only).
    deploy_workspace = self.modal_workspace or _get_modal_workspace()
    if deploy_workspace:
        extra_secrets.append(
            modal.Secret.from_dict({STARDAG_MODAL_WORKSPACE_ENV: deploy_workspace})
        )
    # The deployment's id, baked into every function: the bootstrap, the
    # resident builder and every tick plan under it, a tick compares it
    # with the active plan's to roll a build over, and a worker names it
    # on its yields (the registry refuses one from another deployment).
    extra_secrets.append(
        modal.Secret.from_dict({STARDAG_DEPLOYMENT_ID_ENV: str(self.deployment_id)})
    )
    # The registry API-key secret is injected into every function (build,
    # workers, tick, watchdog) — all of them talk to the registry. It's
    # the ONLY secret propagated across functions; per-function
    # `secrets` stay function-local.
    if self.stardag_api_key_secret is not None:
        if self._api_key_secret_name is not None:
            _validate_api_key_secret(self)
        extra_secrets.append(self.stardag_api_key_secret)
    return extra_secrets


def _register_functions(
    self: "StardagApp",
    *,
    extra_secrets: list[modal.Secret],
    auto_volumes: dict[str, modal.Volume],
    task_module_patterns: tuple[str, ...],
    task_modules: list[str],
) -> list[str]:
    """Register the deployment's functions on ``self.modal_app``; returns
    their names."""

    def register(
        name: str,
        settings: FunctionSettings,
        *,
        default_concurrency: InputConcurrency | None = None,
        never_concurrent: bool = False,
        one_build_per_process: bool = False,
        **extra: typing.Any,
    ):
        """Register one function on the Modal app under ``name``.

        ``default_concurrency`` is stardag's opinion about how this
        particular function should be packed, applied only when the
        app's own settings say nothing — see the ``tick`` registration
        below, which is the one function that has one.

        ``never_concurrent`` refuses input concurrency for this
        function whatever the settings say. Needed because settings
        are shared: ``tick`` and ``tick_watchdog`` are registered from
        one ``tick_settings``, so an app that packs its tick would
        otherwise pack a sync watchdog too — onto Modal's *threads*,
        which is the hazard the async tick exists to avoid.

        ``one_build_per_process`` refuses a declared concurrency above one
        (see ``_TICK_CONCURRENCY``): the function applies a build's
        settings, which are per process.
        """
        prepared = _prepare_function_settings(
            settings,
            extra_secrets=extra_secrets,
            auto_volumes=auto_volumes,
        )
        decorate = self.modal_app.function(
            **{**prepared.kwargs, "name": name, "serialized": True, **extra}
        )
        # Input concurrency is a decorator rather than a `function()`
        # keyword (Modal moved it in April 2025), so it is applied to
        # the callable first and `function()` registers the result.
        concurrency = (
            None if never_concurrent else prepared.concurrency or default_concurrency
        )
        if one_build_per_process:
            _refuse_packed_settings_function(name, concurrency)
        if concurrency is None:
            return decorate

        def decorate_concurrent(fn):
            return decorate(modal.concurrent(**concurrency)(fn))

        return decorate_concurrent

    # Wrap callables in real functions for Modal compatibility.
    # Modal's is_async() only accepts inspect.isfunction()-compatible objects,
    # not callable class instances. The wrappers delegate to the actual callable
    # and are what get serialized/sent to Modal.
    #
    # Every wrapper below opens with _run_container_setup(container_setup):
    # it is the app's one chance to prepare a container, and the top of
    # the wrapper is the only place common to all five functions that
    # runs before any stardag work. _run_container_setup no-ops when the
    # app supplied no hook, and after the first input in this container.
    container_setup = self.container_setup
    build_fn = self._build_function

    def _modal_build(
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        _run_container_setup(container_setup)
        # The deployed module list, as the tick imports it: the resident
        # build's classes resolve the same way everywhere.
        if task_modules:
            set_declared_task_module_patterns(task_module_patterns)
            import_task_modules(task_modules)
        return build_fn(tasks, worker_selector, app_name, build_kwargs=build_kwargs)

    run_fn = self._run_function
    # The ``RunFunction`` protocol gained an optional ``env_overrides``
    # parameter. Older custom run functions implemented the protocol with a
    # bare ``(task)`` signature, so only forward ``env_overrides`` to those
    # that accept it; otherwise apply the overrides in the wrapper.
    run_fn_accepts_env = _callable_accepts_env_overrides(run_fn)

    def _modal_run(
        task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> typing.Any:
        _run_container_setup(container_setup)
        # Publish the app's task-module patterns for the worker-side
        # code that needs them but is nowhere near the app object: the
        # reporter checks the coverage of dynamically yielded deps,
        # which the trigger's pre-flight cannot see. The worker does
        # not IMPORT the modules: its task arrived by value and its
        # dynamic deps were just constructed by user code, so their
        # classes are registered by definition.
        set_declared_task_module_patterns(task_module_patterns)
        # The build's settings ride in ``env_overrides``: hold the process
        # for that build while they are applied (one build per process).
        build_id = (env_overrides or {}).get(STARDAG_BUILD_ID_ENV)
        with (
            settings_owner(build_id)
            if build_id is not None
            else contextlib.nullcontext()
        ):
            if run_fn_accepts_env:
                run_fn_with_env = typing.cast(_RunFunctionWithEnv, run_fn)
                return run_fn_with_env(task, env_overrides=env_overrides)
            with temp_env_vars(env_overrides or {}):
                return run_fn(task)

    register("build", self._builder_settings)(_modal_build)
    function_names = ["build"]

    for worker_name, settings in self._worker_settings.items():
        func_name = f"worker_{worker_name}"
        register(func_name, settings, one_build_per_process=True)(_modal_run)
        function_names.append(func_name)

    # Reactive scheduler tick (see stardag.build.run_tick_aio). Spawned
    # by build_trigger(reactive=True), by workers finishing tasks, and
    # by the optional watchdog below. Idempotent and single-flighted —
    # safe to invoke at any time; no-ops on non-reactive builds.
    #
    # Everything the deployed tick needs from deploy time is bundled
    # here and closed over; the body lives in _tick._run_deployed_tick_aio.
    app_name = self.name
    tick_deployment = _TickDeployment(
        app_name=app_name,
        worker_selector=self.worker_selector,
        limit_key_selector=self.limit_key_selector,
        modal_workspace=self.modal_workspace,
        worker_timeouts=self._worker_timeouts(),
        tick_timeout_seconds=_tick_function_timeout_seconds(
            self._tick_settings, self._builder_settings
        ),
        task_modules=tuple(task_modules),
        task_module_patterns=task_module_patterns,
    )

    # ``async def`` on purpose, and load-bearing. Modal serves
    # concurrent inputs to an async function as asyncio tasks on ONE
    # event loop, and to a sync one on threads — and a tick on its own
    # thread would run its own ``asyncio.run``, i.e. its own loop.
    # ``APIRegistry`` is a process-wide singleton whose ``async_client``
    # is cached per loop and *closed and rebuilt* whenever the running
    # loop differs, so two threaded ticks would tear down each other's
    # in-flight HTTP client. Awaiting the body here keeps every tick in
    # the container on one loop and therefore on one client, which is
    # what makes sharing a container safe rather than merely allowed.
    async def _modal_tick(
        build_id: str,
        tick_kwargs: dict[str, typing.Any] | None = None,
    ) -> dict[str, typing.Any]:
        _run_container_setup(container_setup)
        return await _run_deployed_tick_aio(
            build_id, tick_kwargs, deployment=tick_deployment
        )

    # tick/watchdog default to builder_settings when tick_settings is
    # not given; the api-key secret is in extra_secrets so they get
    # registry credentials regardless of which settings apply.
    tick_settings = self._tick_settings or self._builder_settings
    register(
        "tick",
        tick_settings,
        default_concurrency=_TICK_CONCURRENCY,
        one_build_per_process=True,
    )(_modal_tick)
    function_names.append("tick")

    # Reactive bootstrap (see run_reactive_bootstrap). Spawned by
    # build_trigger(reactive=True) with the root tasks BY VALUE —
    # cloudpickled into the call exactly as build_spawn passes
    # ``tasks=`` to the builder — so the DAG is walked here, next to
    # the mounted target root, instead of on the triggering machine.
    def _modal_bootstrap(
        build_id: str,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        tick_kwargs: dict[str, typing.Any] | None = None,
        settings: dict[str, str] | None = None,
    ) -> dict[str, typing.Any]:
        _run_container_setup(container_setup)
        _setup_logging()
        build_uuid = UUID(build_id)
        task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
        registry = registry_provider.get()
        try:
            result = run_reactive_bootstrap(
                build_uuid,
                task_list,
                registry=registry,
                app_name=app_name,
                tick_kwargs=tick_kwargs,
                # The DEPLOYED module list, frozen here alongside the
                # tick's. The trigger does not supply it, which is what
                # makes the rehydration pre-flight compare the DAG
                # against what the ticks will actually import rather
                # than against the caller's local app definition.
                task_module_patterns=task_module_patterns,
                settings=settings,
            )
        except BaseException as e:
            # The trigger handed this container a RUNNING build and
            # returned. Nothing else will notice it died, so a failed
            # bootstrap must not leave an orphan RUNNING build.
            _fail_build_best_effort(registry, build_uuid, e)
            raise
        # The tick handle is process-local; only the summary crosses
        # back to the caller as the Modal return value.
        return result.summary

    # The bootstrap walks the DAG under ``settings_applied(...)`` as well,
    # so it is held to the same one-build-per-process rule as the tick and
    # the workers.
    register(
        "bootstrap",
        self._bootstrap_settings or self._builder_settings,
        one_build_per_process=True,
    )(_modal_bootstrap)
    function_names.append("bootstrap")

    # Always deployed, scheduled only when a period is set. The sweep is
    # a capability of the app — "tick every running build I own" — and
    # whether it runs on a timer is a separate, cost-driven decision.
    # Deploying it unconditionally is what makes a full sweep one click
    # (or one `modal run`) away on an app that runs no cron, which is
    # the answer to "then how do I recover a stalled build?" when the
    # watchdog is left off.
    def _modal_tick_watchdog() -> None:
        _run_container_setup(container_setup)
        _setup_logging()
        # The sweep spawns one `tick` per build and returns; it does not
        # run them here. The app name is both the listing's scope and
        # where each tick is spawned — see _run_watchdog_sweep.
        _run_watchdog_sweep(registry_provider.get(), app_name)

    # `never_concurrent`, not merely "no default": it shares
    # `tick_settings` with the tick, so a declared value would reach it
    # too. This is its own Modal function with its own containers,
    # receiving one input per watchdog period, so packing would change
    # nothing in the steady state — while quietly opting a `def` into
    # Modal's *threaded* concurrency, which is the hazard
    # `_modal_tick` is a coroutine to avoid. Modal accepts a sync
    # function with `@modal.concurrent` and a `schedule` without
    # complaint, so nothing downstream would have caught it.
    watchdog_schedule: dict[str, typing.Any] = (
        {"schedule": modal.Period(minutes=self.watchdog_period_minutes)}
        if self.watchdog_period_minutes is not None
        else {}
    )
    register(
        "tick_watchdog", tick_settings, never_concurrent=True, **watchdog_schedule
    )(_modal_tick_watchdog)
    function_names.append("tick_watchdog")

    return function_names
