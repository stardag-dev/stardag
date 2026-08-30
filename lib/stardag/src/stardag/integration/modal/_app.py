"""The StardagApp that owns a Modal deployment.

:class:`StardagApp` wraps a ``modal.App``, registers the functions a stardag
deployment consists of (:meth:`StardagApp.finalize`), and is the entry point
for running builds on it (``build_spawn`` / ``build_remote`` /
``build_trigger``).

The bodies of the registered functions mostly live in sibling modules — the
wrappers here are thin closures over what ``finalize`` resolved at deploy
time, because a deployed container never sees the ``StardagApp`` object. See
this package's ``__init__`` for a map of those modules.
"""

from __future__ import annotations

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
    TaskModulesError,
    expand_task_module_patterns,
    set_declared_task_module_patterns,
    validate_task_module_patterns,
)
from stardag.exceptions import StardagError
from stardag.integration.modal._bootstrap import (
    ReactiveDiscovery,
    _advise_uncovered_root_task_modules,
    _fail_build_best_effort,
    run_reactive_bootstrap,
)
from stardag.integration.modal._builder import _default_build
from stardag.integration.modal._container_setup import (
    ContainerSetup,
    _run_container_setup,
    _validate_container_setup,
    _validate_serialized_callable,
)
from stardag.integration.modal._limit_keys import set_deployed_limit_key_selector
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_MODAL_WORKSPACE_ENV,
    _get_modal_environment,
    _get_modal_workspace,
)
from stardag.integration.modal._protocols import (
    BuildFunction,
    RunFunction,
    _callable_accepts_env_overrides,
    _RunFunctionWithEnv,
)
from stardag.integration.modal._runner import _default_run
from stardag.integration.modal._selector import (
    WorkerSelector,
    _default_worker_selector,
)
from stardag.integration.modal._settings import (
    FunctionSettings,
    _prepare_function_settings,
)
from stardag.integration.modal._target import get_default_volume_mount_path
from stardag.integration.modal._tick import (
    LimitKeySelector,
    _run_tick,
    _run_watchdog_sweep,
    _tick_function_timeout_seconds,
    _TickDeployment,
    _validate_tick_kwargs,
)
from stardag.integration.modal._volumes import (
    TargetRootsVolumes,
    get_target_roots_volumes,
)
from stardag.registry._base import NoOpRegistry, registry_provider
from stardag.utils.env import temp_env_vars

logger = logging.getLogger(__name__)


class FinalizeResult(typing.NamedTuple):
    """Result of StardagApp.finalize().

    Attributes:
        volumes: Dict of target root key to Modal Volume instance.
        functions: List of created Modal function names.
        volume_mounts: Dict of mount_path -> volume_name for auto-mounted volumes.
        auto_volumes: Dict of mount_path -> Volume for auto-mounted volumes.
        task_modules: The app's ``task_modules`` patterns expanded to the
            concrete, sorted module list baked into the deployed scheduler
            tick (empty when the app opted out). Surfaced so the CLI can
            report what the deployment will import.
    """

    volumes: dict[str, modal.Volume]
    functions: list[str]
    volume_mounts: dict[str, str] = {}
    auto_volumes: dict[str, modal.Volume] = {}
    task_modules: list[str] = []


class BuildTriggerResult(typing.NamedTuple):
    """Result of :meth:`StardagApp.build_trigger`.

    Attributes:
        build_id: The registry build id minted (or reused) at the trigger
            point. Pass it back to ``build_trigger(..., build_id=...)`` to
            re-attach/resume the same build.
        function_call: The Modal ``FunctionCall`` handle for the one
            invocation this trigger spawned — the ``build`` function for a
            resident build, and the ``bootstrap`` function for a reactive
            one. The exception is ``reactive_discovery="local"``, which
            discovers on the triggering machine and therefore has no
            bootstrap to spawn: the handle is the first ``tick`` instead.
            Call ``.get()`` to block on the result if needed.

            For reactive builds the bootstrap call is the honest handle:
            it is what the trigger actually spawned, and it is the call
            whose failure means the build never started (it discovers the
            DAG, persists it, arms the build and spawns the first tick —
            see :func:`run_reactive_bootstrap`). It is *not* a handle on
            the build: a reactive build outlives it by design, and its
            result is the bootstrap summary, not a ``BuildSummary``. It
            previously carried the first tick's call, which resolved as
            soon as that tick lingered out and said nothing about whether
            the DAG had been registered at all.
    """

    build_id: UUID
    function_call: typing.Any


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
    and opt out (the pickle path still works), rather than baking in a
    module list that would fail to import in every tick container.

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
            "not part of an importable package. Reactive scheduler ticks "
            "will therefore fall back to the build task store's pickles "
            "(which need target-root write access at trigger time and are "
            "invalidated by a redeploy). Declare the modules explicitly — "
            'e.g. task_modules=["my_pkg.tasks.*"] — to let ticks '
            "reconstruct tasks from registry data instead, or pass "
            "task_modules=[] to silence this warning."
        )
        return ()
    return (f"{module_name.split('.')[0]}.*",)


class StardagApp:
    """Wrapper around modal.App for Stardag task execution.

    StardagApp manages the Modal app and its functions for building and
    running Stardag tasks. It supports deferred function creation to allow
    runtime configuration (e.g., profile-based environment variables).

    Lifecycle:
        1. Create StardagApp with settings (functions NOT created yet)
        2. Call finalize() to create the Modal functions (done by CLI on deploy)
        3. After deployment, use build_spawn/build_remote to execute tasks

    Example:
        import modal
        from stardag.integration.modal import StardagApp, FunctionSettings

        # User defines their image with full control
        image = (
            modal.Image.debian_slim()
            .pip_install("stardag", "pandas")
            .add_local_python_source("my_code")
        )

        # Create app (deferred - no functions created yet)
        stardag_app = StardagApp(
            "my-app",
            builder_settings=FunctionSettings(image=image),
            worker_settings={"default": FunctionSettings(image=image)},
        )

        # Deploy with CLI (finalize is called automatically):
        # $ stardag modal deploy my_app.py --profile production

        # After deployment, run tasks:
        stardag_app.build_spawn(my_task)

    Attributes:
        modal_app: The underlying modal.App instance.
        name: The app name.
        is_finalized: Whether finalize() has been called.
        container_setup: The app's container-level setup hook, or None
            (see the ``container_setup`` argument).
        task_modules: The validated task-module patterns (see the
            ``task_modules`` argument); empty when opted out.
        require_pickle_free: Whether a reactive build refuses to fall
            back to writing task pickles.
        reactive_discovery: Where a reactive trigger discovers the DAG
            (``"modal"`` by default; see the argument of the same name).
    """

    def __init__(
        self,
        modal_app_or_name: modal.App | str,
        *,
        build_function: BuildFunction = _default_build,
        run_function: RunFunction = _default_run,
        container_setup: ContainerSetup | None = None,
        builder_settings: FunctionSettings,
        worker_settings: dict[str, FunctionSettings],
        worker_selector: WorkerSelector | None = None,
        tick_settings: FunctionSettings | None = None,
        bootstrap_settings: FunctionSettings | None = None,
        reactive_discovery: ReactiveDiscovery = "modal",
        watchdog_period_minutes: int | None = None,
        limit_key_selector: LimitKeySelector | None = None,
        task_modules: typing.Sequence[str] | None = None,
        require_pickle_free: bool = False,
        modal_workspace: str | None = None,
        stardag_api_key_secret: "modal.Secret | str | None" = "stardag-api-key",
    ):
        """Initialize a StardagApp.

        Args:
            modal_app_or_name: Either a modal.App instance or a string name.
                If a string, a new modal.App will be created with that name.
            build_function: Callable registered as the Modal "build" function.
                Must match the ``BuildFunction`` protocol:
                ``(tasks, worker_selector, app_name) -> BuildSummary | None``.
                Defaults to ``Builder()`` which provides overridable
                ``setup()``/``teardown()``/``build()`` hooks. Subclass
                ``Builder`` for customization, or implement the protocol
                directly.

                Unpickling the serialized ``build`` wrapper in a container
                reaches this callable's defining module — the function
                itself for a plain function, its class for a callable
                instance such as a ``Builder`` subclass — so that module is
                imported there and its module-level code runs. That is a
                property of the ``build`` container only; for setup that
                must run in *every* container the app deploys, pass
                ``container_setup``.

                Being reached by import is also why **it must not be
                defined in the file you deploy**: the CLI loads that file
                under a module name taken from its name (``app.py`` ->
                ``app``), which exists only in the deploying process. See
                ``container_setup`` for the whole rule; it is checked here
                and raises.
            run_function: Callable registered as the Modal worker functions.
                Must match the ``RunFunction`` protocol:
                ``(task) -> None``.
                Defaults to ``Runner()`` which provides overridable
                ``setup()``/``teardown()`` hooks.

                As with ``build_function``, unpickling the serialized
                worker wrapper imports this callable's defining module (or
                its class's) inside every ``worker_*`` container — use that,
                or ``Runner.setup()``, for worker-specific setup such as
                GPU init or library preloading, and ``container_setup``
                for setup every container needs. The same placement rule
                applies: define it in an importable module, not in the
                file you deploy.
            container_setup: Called once per container, at the top of
                **every** function this app registers — ``build``, each
                ``worker_*``, and the reactive ``tick``, ``bootstrap`` and
                ``tick_watchdog`` — before any stardag work and before the
                per-build/per-task ``Builder.setup()`` / ``Runner.setup()``
                hooks. Takes no arguments. Default: nothing to run.

                This is the hook for state a container must have before it
                does anything: credentials materialised onto disk, log
                formatting, environment validation. Without it, whether an
                app's setup code runs at all depends on which of its
                modules a given function's closure happens to drag in —
                dependable for ``build`` and ``worker_*``, which close over
                the callables above, but not for the reactive functions,
                and not at all for ``bootstrap``.

                **It does not replace a custom builder or runner, and they
                do not replace it.** The three hooks have different scopes
                and all three are expected to be used together:

                - ``container_setup`` — the *container*. Once, before
                  anything else, in all five functions. Nothing about a
                  build or a task is in scope here (it takes no arguments).
                - ``Builder.setup(tasks)`` — one *build*, in the ``build``
                  container only. Prep that depends on the roots being
                  built.
                - ``Runner.setup(task)`` — one *task*, before each input a
                  worker serves. Prep that depends on that task.

                The split is not a matter of taste for the reactive
                functions: a ``tick``, ``bootstrap`` or ``tick_watchdog``
                container has no ``Builder`` and no ``Runner`` in it at
                all, so ``container_setup`` is the *only* hook that reaches
                them. Conversely, moving per-task work into
                ``container_setup`` would run it once and then never again
                for the rest of that container's inputs.

                **Once per container, not once per input.** A worker
                serves many tasks and a tick container may be reused;
                stardag holds the guard so apps do not each have to write
                one. A hook that raises propagates and is *not* remembered
                as done — the next input tries again — so a hook that
                fails deterministically fails every input rather than
                letting later ones run un-set-up.

                **Only containers this app deploys.** It is not run by
                ``reactive_discovery="local"``, where discovery happens in
                the *triggering* process: that machine is not a container
                of this app, and writing credentials or reconfiguring root
                logging in someone's shell would be the wrong call. An app
                that both relies on the hook and triggers with
                ``"local"`` has to prepare the triggering process itself.

                **Ordering with logging.** The hook runs before stardag's
                own ``logging.basicConfig`` default, and ``basicConfig``
                no-ops once the root logger has handlers, so a hook that
                configures root logging wins and an app that does not still
                gets the default. A hook that instead configures a
                non-root logger will still see stardag add a root
                ``StreamHandler``.

                **Define it in a module that is importable inside the
                container** — source added via
                ``add_local_python_source(...)`` — and import it into the
                file you deploy. This applies identically to
                ``worker_selector``, ``limit_key_selector`` and the two
                callables above: all five are captured by the serialized
                Modal functions, and cloudpickle stores a module-level
                callable (or the class of a callable instance) as a
                *reference* to its defining module, which the container
                resolves by importing it.

                Defining one in the deploy entry point is the way this
                goes wrong, and it is not inferable from the app's own
                code: ``stardag modal deploy path/to/app.py`` loads that
                file under a module name taken from the file name, so a
                ``def`` written there pickles as ``app.<name>`` and no
                container has a module called ``app``. The deploy
                succeeds and the affected functions then die at hydration
                with ``ModuleNotFoundError``. Passing such a callable
                raises :class:`SerializedCallablePlacementError` here
                instead.

                Importing the hook from your own package is also what
                makes module-level code in the hook's own module run in
                every container.
            builder_settings: Settings for the "build" function. Each
                function's settings are independent — nothing is propagated
                between them, except ``stardag_api_key_secret`` (below) and
                the deploy-time env config the CLI injects.
            worker_settings: Dict of worker name to settings. Must include
                "default". Fully independent per worker.
            worker_selector: Function to select the worker name for each task.
                Defaults to always returning "default" — which means an app
                that declares more than one worker and leaves this unset
                deploys workers nothing can reach, so ``finalize()`` warns
                about it. Passing a selector explicitly, even one that
                always returns ``"default"``, is how to say that is
                intended.

                Carried into the serialized ``build`` and ``tick``
                functions, so it obeys the placement rule under
                ``container_setup``: define it in an importable module of
                your own package, not in the file you deploy.
            tick_settings: Settings for the reactive-scheduling ``tick`` /
                ``tick_watchdog`` functions. Defaults to ``builder_settings``
                when not given.
            bootstrap_settings: Settings for the reactive-scheduling
                ``bootstrap`` function — the container that discovers a
                triggered build's DAG, registers it, persists the task
                store and spawns the first tick. Defaults to
                ``builder_settings`` (**not** ``tick_settings``) when not
                given.

                It needs the same image, secrets and target-root volume
                mounts as the builder — it runs the same discovery a
                resident build does — but it wants its **own timeout**,
                which is the main reason it is a separate function rather
                than work done inside the first tick. The two budgets
                answer different questions: a tick is sized for one
                frontier pass and is expected to be short (its timeout
                also derives the per-pass spawn cap), whereas discovery is
                a single whole-DAG walk whose cost scales with the DAG and
                is paid once per trigger. Folding discovery into the tick
                would force one number to cover both, and shortening the
                tick — normally a good idea — would start killing the
                bootstrap of large DAGs.
            reactive_discovery: Where a reactive trigger discovers the
                DAG. ``"modal"`` (the default) spawns the deployed
                ``bootstrap`` function with the root tasks by value and
                returns immediately; ``"local"`` runs the identical
                bootstrap in the triggering process, which is what
                reactive triggers did before the ``bootstrap`` function
                existed.

                ``"modal"`` is the default because discovery is target-root
                I/O — one existence check per task — and inside Modal a
                ``modalvol://`` root is a mounted filesystem rather than a
                rate-limited API. The same discovery code runs either way,
                in the same order and with the same failure handling; what
                differs is the machine, and therefore the container-level
                preparation around it (see ``container_setup`` below).

                Reach for ``"local"`` when the deployed app predates the
                ``bootstrap`` function, or when the target root is
                reachable from the triggering process but not from the
                Modal app. Note that ``"local"`` also puts the coverage
                pre-flight on the *local* ``task_modules`` rather than the
                deployed one, reinstating the stale-deploy blind spot —
                and that ``container_setup`` does **not** run, because the
                triggering process is not a container this app deployed
                (see that argument). Discovery there runs against whatever
                the triggering process is already configured with.
            watchdog_period_minutes: If set, run the ``tick_watchdog`` sweep
                on this period: one scheduling pass over every running
                reactive build this app owns. The sweep function itself is
                always deployed and can be invoked on demand; the period
                only decides whether it also runs on a timer.

                Default (``None``, no timer) is the right choice for most
                apps. Everything a build normally waits for is pushed to it
                — its own workers finishing, a shared task changing status
                in another build, a concurrency slot freeing, a cancel from
                the UI — and carried by the next scheduler pass anywhere on
                the deployment. What only a timer catches is a worker that
                died without reporting (its execution claim expires with
                nothing to notice), and a change made while nothing on the
                deployment is ticking. A standing sweep polls the registry
                whether or not anything is building — enough to keep a
                scale-to-zero database awake, a cost that does not show up
                as Modal usage — so set it when leaving a build stalled for
                even a few minutes is unacceptable, and pick the period
                from how long that is.
            limit_key_selector: Maps a task to the named registry
                concurrency-limit keys it runs under in reactive scheduling
                (deployed-app configuration applied by every tick). Default:
                no limits.

                Carried into the serialized ``tick`` function, so it obeys
                the placement rule under ``container_setup``: define it in
                an importable module of your own package, not in the file
                you deploy.
            task_modules: Modules whose import registers the task classes
                this app may schedule. **Only reactive scheduling needs
                this**: a scheduler tick reconstructs tasks from registry
                data and can resolve only classes that are already
                registered in its process (registration happens at class
                definition time). Resident builds hold the real task
                objects and are entirely unaffected.

                Each entry is an exact module (``"my_pkg.tasks.ingest"``)
                or a package with a trailing recursive wildcard
                (``"my_pkg.tasks.*"``); anything else raises. The patterns
                are expanded to a concrete module list at ``finalize()``
                and baked into the deployed tick, so **adding or moving
                task classes requires a redeploy**. Declared modules become
                import-hot — they are imported in every tick container —
                so keep heavy runtime dependencies inside ``run()`` rather
                than at module scope.

                Default (``None``): infer ``"<root package of the module
                defining this app>.*"``. Pass ``[]`` to opt out.

                **Declaring this explicitly is also the opt-in to skipping
                pickles** — the inferred default only drives the coverage
                warning. The trigger reads the local app definition while
                the tick runs the deployed one, so if inference alone
                elided, upgrading stardag would start dropping pickles that
                an app deployed by an older version cannot compensate for.
            require_pickle_free: Turn the pickle fallback from a silent
                safety net into a hard error. With ``task_modules``
                covering a build's classes, a reactive build writes no
                task pickles at all and therefore needs no target-root
                *write* access; with this flag, a build that *would* have
                fallen back to pickling fails instead, naming every task
                and why. Off by default (the fallback is what keeps the
                feature additive).

                Enforced by the reactive bootstrap, where the task
                store is written — normally in the ``bootstrap``
                container. It fails loudly: the ``TaskModulesError``
                records a terminal BUILD_FAILED and is re-raised on the
                bootstrap's Modal call, so it surfaces both in the
                registry and on
                ``BuildTriggerResult.function_call.get()``. Dynamic
                dependencies registered from inside a worker apply the
                same elision but never raise: their task has already run,
                and failing its bookkeeping to enforce a storage
                preference would be a strictly worse outcome than one
                extra pickle.
            modal_workspace: Explicit Modal workspace name recorded in the
                executor metadata of triggered builds and started tasks
                (used by the UI for Modal dashboard deep links). Default:
                resolved once from the configured Modal token, best-effort.
            stardag_api_key_secret: The Modal secret carrying the Stardag
                Registry API key, injected into **every** function (build,
                workers, tick, watchdog) — all of them talk to the registry
                (workers self-report their lifecycle). Accepts a
                ``modal.Secret``, a secret *name* (``str``, resolved lazily
                via ``modal.Secret.from_name``), or ``None``.

                The default ``"stardag-api-key"`` is the secret name created
                by the CLI: run ``stardag modal stardag-api-key create`` to
                mint a Stardag API key and sync it into a Modal secret of
                that name (see the Modal how-to). With the secret in place,
                this default works out of the box; a string is used (rather
                than a ``modal.Secret``) so resolution is deferred to deploy
                time. If the named secret does not exist, ``finalize()``
                raises a clear error. Set to ``None`` if you supply the API
                key another way (a custom secret per function, or a
                non-secret mechanism).
        """
        if isinstance(modal_app_or_name, str):
            self.modal_app = modal.App(name=modal_app_or_name)
        else:
            assert isinstance(modal_app_or_name, modal.App)
            assert modal_app_or_name.name is not None
            self.modal_app = modal_app_or_name

        # `is not None` rather than truthiness: a selector is an arbitrary
        # callable, and one whose class defines __bool__/__len__ falsey
        # would otherwise be silently swapped for the default — and, since
        # the warning below keys off declaration, swapped *without* the
        # warning that is supposed to catch exactly that outcome.
        self.worker_selector = (
            worker_selector if worker_selector is not None else _default_worker_selector
        )
        # Whether a selector was *declared*, as opposed to defaulted. Only
        # used for the deploy-time reachability warning in finalize(): an
        # app with several workers and no selector routes everything to
        # "default". Explicitly passing one — even one that always returns
        # "default" — is the way to say that is intended.
        self._worker_selector_declared = worker_selector is not None
        self._build_function = build_function
        self._run_function = run_function
        # The app's container-level setup, closed over by every registered
        # wrapper in finalize(). Deployed-app configuration exactly like
        # worker_selector: captured here, carried into the serialized
        # functions, run once per container.
        if container_setup is not None:
            _validate_container_setup(container_setup)
        self.container_setup = container_setup
        # All five callables share one failure mode that nothing later
        # catches: cloudpickle stores a module-level callable as a
        # reference to its defining module, so one defined in the deploy
        # entry point deploys cleanly and then cannot be hydrated in any
        # container. Checked here, together, rather than per parameter —
        # the constraint is a property of being serialized into the
        # deployed functions, which is exactly what these five have in
        # common.
        for parameter, value in (
            ("build_function", build_function),
            ("run_function", run_function),
            ("container_setup", container_setup),
            ("worker_selector", worker_selector),
            ("limit_key_selector", limit_key_selector),
        ):
            if value is not None:
                _validate_serialized_callable(parameter, value)
        self._builder_settings = builder_settings
        self._worker_settings = worker_settings
        # Reactive scheduling: the "tick" function's settings (defaults to
        # builder_settings) and the optional periodic watchdog sweep that
        # re-ticks running builds (covers lost wake-ups and externally
        # cancelled builds). Set watchdog_period_minutes when using
        # build_trigger(reactive=True).
        self._tick_settings = tick_settings
        # The reactive ``bootstrap`` function's settings. Defaults to
        # builder_settings rather than tick_settings on purpose: the
        # bootstrap does the same whole-DAG discovery a resident build
        # does, and an app that shortened its tick (a sensible thing to
        # do — the tick is one frontier pass) must not thereby shorten
        # the budget for discovering a large DAG.
        self._bootstrap_settings = bootstrap_settings
        # Where a reactive trigger discovers the DAG. Deployment-level
        # configuration rather than a per-trigger flag, and deliberately
        # so: the reasons to opt out are properties of the deployment (an
        # app deployed before the bootstrap function existed; a target
        # root the Modal app cannot reach), not of one invocation — and
        # this is where every other reactive knob already lives.
        if reactive_discovery not in typing.get_args(ReactiveDiscovery):
            raise ValueError(
                f"reactive_discovery must be one of "
                f"{list(typing.get_args(ReactiveDiscovery))}, got "
                f"{reactive_discovery!r}"
            )
        self.reactive_discovery: ReactiveDiscovery = reactive_discovery
        self.watchdog_period_minutes = watchdog_period_minutes
        # Maps a task to the named concurrency-limit keys it runs under in
        # reactive scheduling (see the registry's environment concurrency
        # limits). Deployed-app configuration — captured by the tick
        # function at finalize() so every tick of every build applies it
        # consistently (callables can't be persisted in the JSON build
        # meta like the scalar tick_kwargs).
        self.limit_key_selector = limit_key_selector
        # Task-module declaration for reactive scheduling: the patterns
        # whose expansion is imported by every scheduler tick so it can
        # rebuild task objects from registry data (see
        # stardag.build._task_modules). Validated eagerly — a malformed
        # pattern must fail here, not silently match nothing and surface
        # hours later as a tick that cannot reconstruct a task.
        #
        # Whether the patterns were *declared* or merely inferred decides
        # whether pickles may be elided — see _persist_discovered_tasks.
        # Inference must stay observation-only: it happens on every app,
        # including apps written before this feature existed.
        self._task_modules_declared = task_modules is not None
        if task_modules is None:
            task_modules = _infer_task_module_patterns()
        self.task_modules: tuple[str, ...] = validate_task_module_patterns(task_modules)
        if require_pickle_free and not self.task_modules:
            raise TaskModulesError(
                "require_pickle_free=True is meaningless without "
                "task_modules: with no declared modules, no task can be "
                "reconstructed from registry data and every task would "
                "need a pickle. Declare task_modules explicitly (if you "
                "left it at the default, inference was not possible — see "
                "the warning above), or drop require_pickle_free."
            )
        self.require_pickle_free = require_pickle_free
        # Explicit Modal workspace name for executor metadata (UI deep
        # links). Default: resolved from the Modal token, best-effort.
        # Used by build_trigger and by the tick's executor; the resident
        # build function's executor resolves it in-container instead.
        self.modal_workspace = modal_workspace
        # The registry-API-key secret, injected into every function at
        # finalize(). A string is resolved lazily to modal.Secret.from_name
        # (default defers resolution to deploy time); its existence is
        # validated in finalize() with a clear error. None disables it.
        if isinstance(stardag_api_key_secret, str):
            self._api_key_secret_name: str | None = stardag_api_key_secret
            self.stardag_api_key_secret: "modal.Secret | None" = modal.Secret.from_name(
                stardag_api_key_secret
            )
        else:
            self._api_key_secret_name = None
            self.stardag_api_key_secret = stardag_api_key_secret
        self._is_finalized = False

    @property
    def is_finalized(self) -> bool:
        """Whether the app has been finalized (functions created)."""
        return self._is_finalized

    @property
    def name(self) -> str:
        """The Modal app name."""
        assert self.modal_app.name is not None
        return self.modal_app.name

    # --- finalize (deploy) ---

    @staticmethod
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

    def _validate_api_key_secret(self) -> None:
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
            secret_name_flag = (
                "" if name == "stardag-api-key" else f" --secret-name {name}"
            )
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
        self,
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
        # The registry API-key secret is injected into every function (build,
        # workers, tick, watchdog) — all of them talk to the registry. It's
        # the ONLY secret propagated across functions; per-function
        # `secrets` stay function-local.
        if self.stardag_api_key_secret is not None:
            if self._api_key_secret_name is not None:
                self._validate_api_key_secret()
            extra_secrets.append(self.stardag_api_key_secret)
        return extra_secrets

    def _check_worker_routing(self) -> None:
        """Check at deploy that tasks can reach the workers being deployed.

        Two failure shapes, and the difference in severity is the whole
        point. With no ``worker_selector`` every task routes to
        ``"default"``:

        - **No ``"default"`` worker at all** — nothing works. Every task
          routes to a function this app does not deploy, so the deployment
          is dead on arrival. Raises.
        - **A ``"default"`` plus other tiers** — everything works, on the
          wrong worker. The build succeeds, so the symptom is
          indistinguishable from a healthy deployment, which is exactly
          why it is worth one line at the only moment someone is watching.
          Warns.

        The error is scoped to the case where no selector was declared. An
        app that declares one is free to omit ``"default"`` and route
        everything to its own tiers — that works today, and refusing it
        would break a working deployment to enforce a naming convention.

        Deliberately in ``finalize()`` rather than ``__init__``: the app
        object is also constructed in the *triggering* process, which has
        no business being told about the deployment's configuration.

        The warning is not an error because per-trigger overrides
        (``build_spawn(tasks, worker_selector=...)``) are a legitimate way
        to route a resident build. They are not available to reactive
        builds, though, which is why the app-level selector is the answer
        being pointed at.
        """
        if self._worker_selector_declared:
            return
        if "default" not in self._worker_settings:
            declared = sorted(self._worker_settings) or ["<none>"]
            raise StardagError(
                f"StardagApp {self.name!r} has no 'default' worker and no "
                f"worker_selector, so every task would route to a "
                f"'worker_default' function this app does not deploy. "
                f"Declared workers: {', '.join(declared)}. Either add a "
                f"'default' entry to worker_settings, or pass "
                f"worker_selector=... so tasks are routed to the workers "
                f"that do exist (see WorkerSelectorByName)."
            )
        if len(self._worker_settings) <= 1:
            return
        extra = sorted(name for name in self._worker_settings if name != "default")
        logger.warning(
            f"StardagApp {self.name!r} declares {len(self._worker_settings)} "
            f"workers but no worker_selector, so every task routes to "
            f"'default' and these are unreachable: {', '.join(extra)}. Pass "
            f"worker_selector=... to StardagApp (see WorkerSelectorByName). "
            f"Per-trigger overrides — build_spawn/build_trigger("
            f"worker_selector=...) — cover resident builds only; reactive "
            f"builds reject them, because later ticks could not honour "
            f"them. If routing everything to 'default' is intended, pass a "
            f"selector that says so to silence this."
        )

    def _worker_timeouts(self) -> dict[str, int]:
        """Per-worker Modal ``timeout``, as declared in ``worker_settings``.

        Captured at finalize() because this is the only place they exist:
        a tick runs in a deployed container with no access to the app
        object, and the claim TTL it records for a task is derived from the
        timeout of the worker that task routes to. Workers without an
        explicit timeout are simply absent (Modal's own default is not a
        promise this SDK should encode).
        """
        return {
            worker_name: timeout
            for worker_name, settings in self._worker_settings.items()
            if (timeout := settings.get("timeout")) is not None
        }

    def finalize(
        self,
        *,
        extra_secrets: list[modal.Secret] | None = None,
        create_volumes_if_missing: bool = True,
    ) -> FinalizeResult:
        """Finalize the app by creating Modal functions.

        This method creates the builder and worker functions on the Modal app.
        It should be called before deployment, typically by the CLI.

        Discovered Modal volumes from target roots are automatically mounted at
        /mnt/stardag-volumes/<volume-name> and the STARDAG_MODAL_VOLUME_MOUNTS
        env var is set so that ModalMountedVolumeFileTarget (local I/O) is used
        instead of ModalVolumeRemoteFileSystem (API-based).

        Args:
            extra_secrets: Additional secrets to inject into all functions.
                This is where profile-based environment variables are injected.
            create_volumes_if_missing: Whether to create Modal volumes for
                target roots if they don't exist.

        Returns:
            FinalizeResult with created volumes, function names, mount info,
            and the expanded ``task_modules`` baked into the tick.

        Raises:
            RuntimeError: If finalize() has already been called.
            TaskModulesError: If a ``task_modules`` pattern cannot be
                expanded (e.g. its root package is not importable here).
        """
        if self._is_finalized:
            raise RuntimeError("StardagApp has already been finalized")

        self._check_worker_routing()

        # Discover and create Modal volumes from target roots
        target_roots_volumes = get_target_roots_volumes(
            create_if_missing=create_volumes_if_missing
        )
        volume_mounts, auto_volumes = self._auto_mounted_volumes(target_roots_volumes)
        extra_secrets = self._resolve_extra_secrets(extra_secrets, volume_mounts)

        # Expand the declared task-module patterns to a concrete, sorted
        # module list ONCE, here, and bake it into the deployed functions
        # below. Deploy-time expansion (rather than in-container) keeps the
        # deployed set explicit and auditable, keeps container startup off
        # the filesystem, and makes the deployment reproducible: a module
        # added after this deploy is not silently picked up by a running
        # tick — it needs a redeploy, which is also when the operator gets
        # to see the list change. Only name expansion happens here (no
        # submodule imports): the CLI does the optional local import check.
        task_module_patterns = self.task_modules
        task_modules = expand_task_module_patterns(task_module_patterns)

        def register(name: str, settings: FunctionSettings, **extra: typing.Any):
            """Register one function on the Modal app under ``name``."""
            prepared = _prepare_function_settings(
                settings,
                extra_secrets=extra_secrets,
                auto_volumes=auto_volumes,
            )
            return self.modal_app.function(
                **{**prepared, "name": name, "serialized": True, **extra}
            )

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
            return build_fn(tasks, worker_selector, app_name, build_kwargs=build_kwargs)

        run_fn = self._run_function
        limit_key_selector = self.limit_key_selector
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
            # code that needs them but is nowhere near the app object:
            # _WorkerLifecycleReporter._register_dynamic_deps checks the
            # coverage of dynamically yielded deps, which the trigger's
            # pre-flight cannot see. The worker does not IMPORT the
            # modules: its task arrived by value (self-importing) and its
            # dynamic deps were just constructed by user code, so their
            # classes are registered by definition.
            set_declared_task_module_patterns(task_module_patterns)
            # Likewise the app's concurrency-limit key selector, so the
            # dynamic deps a worker registers carry their keys.
            set_deployed_limit_key_selector(limit_key_selector)
            if run_fn_accepts_env:
                run_fn_with_env = typing.cast(_RunFunctionWithEnv, run_fn)
                return run_fn_with_env(task, env_overrides=env_overrides)
            with temp_env_vars(env_overrides or {}):
                return run_fn(task)

        register("build", self._builder_settings)(_modal_build)
        function_names = ["build"]

        for worker_name, settings in self._worker_settings.items():
            func_name = f"worker_{worker_name}"
            register(func_name, settings)(_modal_run)
            function_names.append(func_name)

        # Reactive scheduler tick (see stardag.build.run_tick_aio). Spawned
        # by build_trigger(reactive=True), by workers finishing tasks, and
        # by the optional watchdog below. Idempotent and single-flighted —
        # safe to invoke at any time; no-ops on non-reactive builds.
        #
        # Everything the deployed tick needs from deploy time is bundled
        # here and closed over; the body lives in _tick._run_tick.
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

        def _modal_tick(
            build_id: str,
            tick_kwargs: dict[str, typing.Any] | None = None,
        ) -> dict[str, typing.Any]:
            _run_container_setup(container_setup)
            return _run_tick(build_id, tick_kwargs, deployment=tick_deployment)

        # tick/watchdog default to builder_settings when tick_settings is
        # not given; the api-key secret is in extra_secrets so they get
        # registry credentials regardless of which settings apply.
        tick_settings = self._tick_settings or self._builder_settings
        register("tick", tick_settings)(_modal_tick)
        function_names.append("tick")

        # Reactive bootstrap (see run_reactive_bootstrap). Spawned by
        # build_trigger(reactive=True) with the root tasks BY VALUE —
        # cloudpickled into the call exactly as build_spawn passes
        # ``tasks=`` to the builder — so the DAG is walked here, next to
        # the mounted target root, instead of on the triggering machine.
        elide_pickles = self._task_modules_declared or self.require_pickle_free
        require_pickle_free = self.require_pickle_free

        def _modal_bootstrap(
            build_id: str,
            tasks: typing.Sequence[BaseTask] | BaseTask,
            tick_kwargs: dict[str, typing.Any] | None = None,
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
                    # The DEPLOYED module list and elision opt-in, frozen
                    # here alongside the tick's. The trigger does not
                    # supply them, which is what makes the coverage
                    # pre-flight compare the DAG against what the ticks
                    # will actually import rather than against the
                    # caller's local app definition.
                    task_module_patterns=task_module_patterns,
                    elide_pickles=elide_pickles,
                    require_pickle_free=require_pickle_free,
                    limit_key_selector=tick_deployment.limit_key_selector,
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

        register("bootstrap", self._bootstrap_settings or self._builder_settings)(
            _modal_bootstrap
        )
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
            # The watchdog runs on the same settings as `tick`, so its
            # container has the same timeout — which it then splits
            # across the builds it sweeps (see _run_watchdog_sweep).
            # Scoped to this app's own reactive builds, and handed the
            # container's own timeout to split across them — see
            # _run_watchdog_sweep for both.
            _run_watchdog_sweep(
                registry_provider.get(),
                _modal_tick,
                tick_timeout_seconds=tick_deployment.tick_timeout_seconds,
                reactive_app_name=app_name,
            )

        register(
            "tick_watchdog",
            tick_settings,
            **(
                {"schedule": modal.Period(minutes=self.watchdog_period_minutes)}
                if self.watchdog_period_minutes is not None
                else {}
            ),
        )(_modal_tick_watchdog)
        function_names.append("tick_watchdog")

        self._is_finalized = True

        return FinalizeResult(
            volumes=target_roots_volumes.by_root_key,
            functions=function_names,
            volume_mounts=volume_mounts,
            auto_volumes=auto_volumes,
            task_modules=task_modules,
        )

    # --- Running builds on the deployed app ---

    def build_spawn(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Spawn a build job on a deployed Modal app (non-blocking).

        This method looks up the deployed "build" function by name and spawns
        a new execution. Use this for fire-and-forget builds.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            A Modal FunctionCall handle for the spawned build.

        Example:
            handle = stardag_app.build_spawn(my_task)
            # Or multiple roots:
            handle = stardag_app.build_spawn([task_a, task_b])
            # Optionally wait for result:
            result = handle.get()
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def build_trigger(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
        build_id: UUID | None = None,
        description: str | None = None,
        reactive: bool = False,
        tick_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildTriggerResult:
        """Trigger a build with a registry build id minted at the trigger point.

        Unlike :meth:`build_spawn` — where the build id is created *inside*
        the Modal build container — this method first creates (or reuses) the
        build in the registry from the calling process, then spawns the
        deployed build function with ``resume_build_id`` set to it. As a
        result:

        - Any restart of the build function (Modal retry after preemption, a
          manual re-trigger with the returned ``build_id``) **resumes the
          same build** instead of creating a new one: already-completed task
          targets are detected during discovery and skipped.
        - The build appears in the registry immediately, before the Modal
          container has started.

        Set ``retries`` in the app's ``builder_settings`` to let Modal
        automatically re-run (and thereby resume) the build function after
        infrastructure failures.

        Requires registry credentials in the calling process (the active
        stardag profile), in addition to Modal credentials. If no registry is
        configured, use :meth:`build_spawn` instead.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build
                function (must not contain ``resume_build_id``; it is set by
                this method).
            build_id: Existing build id to re-attach to (e.g. from a previous
                ``build_trigger`` call). If None, a new build is created.
            description: Optional description for the new build (ignored when
                ``build_id`` is given).
            reactive: **Experimental.** Schedule the build reactively (no
                resident orchestrator): the deployed ``bootstrap`` function
                discovers the DAG, registers it and persists the task
                objects *inside Modal*, then short-lived scheduler *ticks*
                (spawned by the bootstrap, by workers finishing tasks, and
                by the optional watchdog) drive the build — see
                ``stardag.build.run_tick_aio`` for semantics and current
                limitations. Requires the app to be deployed with this
                stardag version (the ``bootstrap`` and ``tick`` functions
                and self-reporting workers) and registry credentials in the
                calling process — but **no target-root access**: this call
                mints the build, registers the roots and spawns, and
                performs no target I/O at all (unless the app opted out
                with ``reactive_discovery="local"``). Re-trigger with the
                returned ``build_id`` to wake a stalled build or add new
                root tasks to it.
            tick_kwargs: Optional kwargs for the reactive ``TickConfig``
                (e.g. ``{"linger_seconds": 30}``).

        Returns:
            BuildTriggerResult with the ``build_id`` and the spawned Modal
            ``FunctionCall`` handle (the ``build`` function, or the
            ``bootstrap`` function when ``reactive=True``).
        """
        merged_kwargs = dict(build_kwargs or {})
        if "resume_build_id" in merged_kwargs:
            raise TypeError(
                "build_kwargs must not contain 'resume_build_id'; pass "
                "build_id=... to build_trigger instead"
            )
        if reactive and merged_kwargs:
            raise TypeError(
                "build_kwargs are not supported with reactive=True (there is "
                "no resident build function); use tick_kwargs for TickConfig "
                "options"
            )
        if reactive and worker_selector is not None:
            raise TypeError(
                "worker_selector overrides are not supported with "
                "reactive=True: later scheduler ticks (worker wake-ups, "
                "watchdog) always use the app's deployed worker_selector, so "
                "a per-trigger override would change routing mid-build. "
                "Configure the selector on StardagApp instead."
            )
        if reactive:
            tick_kwargs = _validate_tick_kwargs(tick_kwargs)

        registry = registry_provider.get()
        # A configured registry is needed to mint a new build id, and
        # always in reactive mode: the registry IS the scheduler state,
        # and the roots are registered here before anything is spawned.
        if (build_id is None or reactive) and isinstance(registry, NoOpRegistry):
            raise RuntimeError(
                "build_trigger requires a configured registry to mint the "
                "build id at the trigger point (run 'stardag auth login' "
                "or configure an API key). Use build_spawn to trigger a "
                "build without local registry credentials."
            )
        task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
        explicit_build_id = build_id is not None
        executor_metadata = self._build_executor_metadata(reactive=reactive)
        if build_id is None:
            build_id = registry.build_start(
                root_tasks=task_list,
                description=description,
                executor_metadata=executor_metadata,
            )

        if reactive:
            return self._trigger_reactive(
                task_list,
                build_id=build_id,
                registry=registry,
                tick_kwargs=tick_kwargs,
                is_retrigger=explicit_build_id,
                executor_metadata=executor_metadata,
            )

        merged_kwargs["resume_build_id"] = build_id
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        function_call = build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=merged_kwargs,
        )
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def _build_executor_metadata(self, *, reactive: bool) -> dict[str, typing.Any]:
        """Build-level executor metadata for a trigger (best-effort).

        ``function_name`` is the function the trigger actually spawns, not
        the one that does most of the work afterwards: operator and UI
        surfaces render it as "what was invoked", and a reactive build
        discovered in Modal is spawned as ``bootstrap`` (which then arms the
        build and spawns the first tick). Naming ``tick`` there would send a
        reader looking through the wrong function's logs for the failure
        that stopped the build from starting.
        """
        if not reactive:
            spawned = "build"
        elif self.reactive_discovery == "local":
            # Local discovery skips the bootstrap and spawns the first tick
            # directly, so `tick` is the honest answer in that mode.
            spawned = "tick"
        else:
            spawned = "bootstrap"
        metadata: dict[str, typing.Any] = {
            "kind": MODAL_EXECUTOR_NAME,
            "app_name": self.name,
            "function_name": spawned,
            "reactive": reactive,
        }
        try:
            workspace = self.modal_workspace or _get_modal_workspace()
            if workspace:
                metadata["workspace"] = workspace
            environment = _get_modal_environment()
            if environment:
                metadata["environment"] = environment
        except Exception:
            logger.debug(
                "Failed to resolve Modal workspace/environment for "
                "build executor metadata",
                exc_info=True,
            )
        return metadata

    def _trigger_reactive(
        self,
        task_list: list[BaseTask],
        *,
        build_id: UUID,
        registry: typing.Any,
        tick_kwargs: dict[str, typing.Any] | None,
        is_retrigger: bool,
        executor_metadata: dict[str, typing.Any] | None = None,
    ) -> BuildTriggerResult:
        """Reactive trigger: register the roots, then spawn ``bootstrap``.

        Everything expensive happens in Modal. What is left here is the
        work that either costs no target I/O or must precede any spawn:

        - mint (or resume) the build, so it exists before a container does;
        - register the roots server-side. This is genuinely free of target
          I/O — ``_get_task_data_for_registration`` reads ``target().uri``,
          which *constructs* a URI and performs no existence check;
        - spawn ``bootstrap`` with the roots **by value** (cloudpickled
          into the call, exactly as :meth:`build_spawn` passes ``tasks=``
          to the builder) and return.

        The DAG walk, the task-module coverage pre-flight, the task-store
        writes, the reactive marker and the first tick all live in the
        bootstrap container — see :func:`run_reactive_bootstrap`, which
        also documents the ordering guarantee that keeps a tick from ever
        seeing a partially-registered DAG. Triggering is therefore fast
        and touches no target root, which for a ``modalvol://`` root is
        the difference between one spawn and one rate-limited volume API
        call per task in the DAG.

        With ``StardagApp(reactive_discovery="local")`` the very same
        :func:`run_reactive_bootstrap` runs here instead, against the
        local app's task-module list. Everything below — ordering,
        failure handling, re-trigger semantics — is identical either way;
        only the machine changes.

        Re-triggering an existing build id is fully supported:

        - The build is *resumed* (BUILD_RESUMED) so a terminal build —
          including a FAILED one — becomes RUNNING again and ticks act on
          it (they bail on terminal statuses otherwise).
        - The passed roots are appended to the build's ``root_task_ids``
          server-side, so terminal detection covers them (previously,
          completion of the original roots would strand re-triggered
          subtrees silently).
        - Previously failed/cancelled/skipped tasks in the (re-)discovered
          DAG are reset to pending (``retry_failed``, applied by the
          bootstrap's discovery) — the retry path for reactive builds.
        - The reactive metadata is updated in the registry: because the
          registry is mutable (unlike an immutable target root), a
          re-trigger MAY change ``tick_kwargs`` (a bare re-trigger with no
          explicit tick_kwargs preserves the existing ones).

        ``tick_kwargs`` ride along to the bootstrap, which persists them in
        the build's ``reactive_tick_kwargs`` so that EVERY tick — including
        worker wake-ups and watchdog sweeps, which spawn with only the
        build id — runs with the same configuration.

        **No orphan RUNNING builds.** Once this trigger knows the build is
        RUNNING, any failure before the bootstrap is airborne records a
        terminal BUILD_FAILED before propagating. The arming point is
        deliberate: on a re-trigger the build may still be *terminal* until
        ``build_resume`` succeeds, and failing a build that this trigger
        never managed to resume would be a lie about which attempt died.
        Failures on the other side of the spawn are the bootstrap's to
        report, and it does.
        """
        root_ids = [str(t.id) for t in task_list]
        if is_retrigger:
            # Un-terminal the build (no-op on a fresh/running build).
            # Deliberately OUTSIDE the failure guard below: until this
            # succeeds the build may still be terminal, and marking a
            # terminal build failed on behalf of a resume that never
            # landed would misattribute someone else's outcome.
            registry.build_resume(build_id, executor_metadata=executor_metadata)
        # From here the build is RUNNING (fresh builds since build_start,
        # re-triggers since the resume above) and this trigger owns it.
        try:
            if is_retrigger:
                # Register the (possibly new) roots BEFORE the bootstrap is
                # spawned, so a concurrent tick can't complete-and-terminal
                # the build on the old root set while the new subtree is
                # still being discovered.
                registry.build_add_roots(build_id, root_ids)
            if self.reactive_discovery == "local":
                # No advisory here: the authoritative check is about to
                # run in this very process, microseconds from now. Two
                # messages saying overlapping things would be noise.
                return BuildTriggerResult(
                    build_id=build_id,
                    function_call=run_reactive_bootstrap(
                        build_id,
                        task_list,
                        registry=registry,
                        app_name=self.name,
                        tick_kwargs=tick_kwargs,
                        task_module_patterns=self.task_modules,
                        elide_pickles=(
                            self._task_modules_declared or self.require_pickle_free
                        ),
                        require_pickle_free=self.require_pickle_free,
                        limit_key_selector=self.limit_key_selector,
                    ).tick_call,
                )
            # Early, roots-only advisory (see the function's docstring):
            # additive feedback in the operator's terminal, never the
            # coverage check itself — that one runs over the full
            # discovered DAG inside run_reactive_bootstrap. Only worth
            # emitting when the real check lands in another process's
            # logs, i.e. exactly here.
            _advise_uncovered_root_task_modules(task_list, self.task_modules)
            bootstrap_function = modal.Function.from_name(
                app_name=self.name, name="bootstrap"
            )
            function_call = bootstrap_function.spawn(
                build_id=str(build_id),
                tasks=task_list,
                tick_kwargs=tick_kwargs,
            )
        except BaseException as e:
            _fail_build_best_effort(registry, build_id, e)
            raise
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def build_remote(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Run a build on a deployed Modal app (blocking).

        This method looks up the deployed "build" function by name and runs
        it synchronously. Use this when you need to wait for the build result.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            The result of the build.

        Example:
            result = stardag_app.build_remote(my_task)
            # Or multiple roots:
            result = stardag_app.build_remote([task_a, task_b])
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.remote(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def local_entrypoint(self, *args, **kwargs):
        """Create a local entrypoint on the underlying Modal app.

        This is a passthrough to modal.App.local_entrypoint().
        """
        return self.modal_app.local_entrypoint(*args, **kwargs)
