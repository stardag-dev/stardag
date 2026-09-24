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

import logging
import typing
import warnings
from uuid import UUID

import modal

from stardag.build._deployment import (
    code_id as _process_code_id,
)
from stardag.build._registration import new_id
from stardag.build._task_modules import (
    expand_task_module_patterns,
    validate_task_module_patterns,
)
from stardag.exceptions import StardagError
from stardag.integration.modal._bootstrap import (
    ReactiveDiscovery,
)
from stardag.integration.modal._builder import _default_build
from stardag.integration.modal._container_setup import (
    ContainerSetup,
    _validate_container_setup,
    _validate_serialized_callable,
)
from stardag.integration.modal._protocols import (
    BuildFunction,
    RunFunction,
)
from stardag.integration.modal._runner import _default_run
from stardag.integration.modal._selector import (
    WorkerSelector,
    _default_worker_selector,
)
from stardag.integration.modal._settings import (
    FunctionSettings,
)
from stardag.integration.modal._tick import (
    LimitKeySelector,
)
from stardag.integration.modal._functions import (
    _auto_mounted_volumes,
    _infer_task_module_patterns,
    _register_functions,
    _resolve_extra_secrets,
)
from stardag.integration.modal._trigger import _Triggering
from stardag.integration.modal._volumes import (
    get_target_roots_volumes,
)

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


class StardagApp(_Triggering):
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
            ``task_modules`` argument); empty when opted out, which makes
            the app resident-only.
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
                One live deployment per name, as on Modal: every
                ``stardag modal deploy`` is a new deployment (a new scope),
                and a running reactive build rolls over to it at its next
                scheduler tick (``docs/design/registry-v2/design.md``,
                "Rollover"). A branch or experiment that must not take over
                production is simply another app name.
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
                defining this app>.*"``. Pass ``[]`` to opt out — which
                makes the app **resident-only**, since a reactive trigger
                on an app with no task modules is refused.

                **This is a precondition, not a preference.** There is no
                second way for a tick to get a task object, so the reactive
                bootstrap runs a dry run of the reconstruction over the
                whole discovered DAG and **refuses the build** if any task
                fails it, naming each class and the pattern that would
                cover it. A class the deployment cannot import is a task
                nothing could ever schedule; saying so at the trigger beats
                discovering it one stalled task at a time.
            require_pickle_free: **Deprecated and ignored.** Reactive
                builds no longer write task pickles at all — the build task
                store is retired and registry data is the only task
                representation — so what this flag used to ask for is
                simply how it works. Passing it (either value) emits a
                ``DeprecationWarning`` and changes nothing; remove it.
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
        # The code identity recorded with the deployment, and the
        # deployment's id, baked into every function at finalize().
        self._code_id: str | None = None
        self._deployment_id: UUID | None = None

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
        # hours later as a build refused for classes the user believes
        # they declared.
        #
        # Declared and inferred patterns are the same thing now. They used
        # to differ, because inference gated pickle elision and an SDK
        # upgrade must not start dropping pickles on an app's behalf;
        # with no pickles to drop there is nothing left for the
        # distinction to gate, and an app whose tasks live under its own
        # root package is exactly the app the inferred pattern serves.
        if task_modules is None:
            task_modules = _infer_task_module_patterns()
        self.task_modules: tuple[str, ...] = validate_task_module_patterns(task_modules)
        if require_pickle_free:
            warnings.warn(
                "StardagApp(require_pickle_free=...) is deprecated and "
                "ignored: reactive builds no longer write task pickles at "
                "all (the build task store is retired), so every reactive "
                "build is pickle-free by construction. Remove the argument.",
                DeprecationWarning,
                stacklevel=2,
            )
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
        """The Modal app name this object deploys as, or was deployed as."""
        assert self.modal_app.name is not None
        return self.modal_app.name

    @property
    def code_id(self) -> str:
        """The code identity of this process (``STARDAG_CODE_ID``, else the
        clean git SHA, else a one-off uuid), recorded with the deployment."""
        if self._code_id is None:
            self._code_id = _process_code_id()
        return self._code_id

    @property
    def deployment_id(self) -> UUID:
        """The id of the deployment this app object finalizes into: minted
        by ``stardag modal deploy`` (and registered before the deploy), or
        here on first use. Baked into every function as
        ``STARDAG_DEPLOYMENT_ID``."""
        if self._deployment_id is None:
            self._deployment_id = new_id()
        return self._deployment_id

    # --- finalize (deploy) ---

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
        deployment_id: UUID | None = None,
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
            deployment_id: The deployment's id, minted and registered by
                ``stardag modal deploy`` before the deploy; minted here if
                not given (such a deployment is unknown to the registry
                until it is recorded, so its ticks cannot plan).

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
        if deployment_id is not None:
            self._deployment_id = deployment_id

        self._check_worker_routing()

        # Discover and create Modal volumes from target roots
        target_roots_volumes = get_target_roots_volumes(
            create_if_missing=create_volumes_if_missing
        )
        volume_mounts, auto_volumes = _auto_mounted_volumes(target_roots_volumes)
        extra_secrets = _resolve_extra_secrets(self, extra_secrets, volume_mounts)

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

        function_names = _register_functions(
            self,
            extra_secrets=extra_secrets,
            auto_volumes=auto_volumes,
            task_module_patterns=task_module_patterns,
            task_modules=task_modules,
        )
        self._is_finalized = True

        return FinalizeResult(
            volumes=target_roots_volumes.by_root_key,
            functions=function_names,
            volume_mounts=volume_mounts,
            auto_volumes=auto_volumes,
            task_modules=task_modules,
        )

    def local_entrypoint(self, *args, **kwargs):
        """Create a local entrypoint on the underlying Modal app.

        This is a passthrough to modal.App.local_entrypoint().
        """
        return self.modal_app.local_entrypoint(*args, **kwargs)
