"""Running builds on a deployed app: ``build_spawn`` / ``build_trigger`` /
``build_remote`` (a mixin of :class:`~._app.StardagApp`).

A trigger mints the build in the registry from the calling process — its
root task ids are the build's request, fixed for its life — and spawns the
deployed function that drives it: the resident ``build`` function, or the
reactive ``bootstrap`` (which plans the build under its deployment, arms it
and spawns the first tick). The trigger does no target I/O at all, unless
the app discovers locally (``reactive_discovery="local"``).

``settings`` (D4) are validated here, before anything is created: a flat
``dict[str, str]`` of environment variables for every process of the build,
``STARDAG_*`` / ``MODAL_*`` keys refused.
"""

from __future__ import annotations

import logging
import typing
from uuid import UUID

import modal

from stardag import BaseTask
from stardag.build._registration import new_id
from stardag.build._settings import resolve_settings, validate_settings
from stardag.build._task_modules import TaskModulesError
from stardag.integration.modal._bootstrap import (
    ReactiveDiscovery,
    _advise_uncovered_root_task_modules,
    _fail_build_best_effort,
    run_reactive_bootstrap,
)
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    _get_modal_environment,
    _get_modal_workspace,
)
from stardag.integration.modal._selector import WorkerSelector
from stardag.integration.modal._tick import _validate_tick_kwargs
from stardag.registry import RegistryABC, is_noop_registry, registry_provider

logger = logging.getLogger(__name__)


class BuildTriggerResult(typing.NamedTuple):
    """Result of :meth:`StardagApp.build_trigger`.

    Attributes:
        build_id: The registry build id minted (or reused) at the trigger.
            Pass it back as ``build_trigger(..., build_id=...)`` to resume the
            same build (same roots — a build is one request).
        function_call: The Modal ``FunctionCall`` of the one invocation the
            trigger spawned: the ``build`` function for a resident build,
            the ``bootstrap`` function for a reactive one — or, with
            ``reactive_discovery="local"``, the first ``tick``. A reactive
            build outlives its bootstrap by design; the bootstrap's result
            is its summary, not a ``BuildSummary``.
    """

    build_id: UUID
    function_call: typing.Any


class _Triggering:
    """The trigger methods of :class:`~._app.StardagApp`."""

    # Provided by StardagApp.
    worker_selector: WorkerSelector
    task_modules: tuple[str, ...]
    reactive_discovery: ReactiveDiscovery
    modal_workspace: str | None

    if typing.TYPE_CHECKING:

        @property
        def name(self) -> str: ...

    def build_spawn(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Spawn a resident build on the deployed app (non-blocking); the
        build is created inside the ``build`` container.

        Returns:
            The Modal ``FunctionCall`` of the spawned build.
        """
        build_function = modal.Function.from_name(app_name=self.name, name="build")
        return build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def build_remote(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Run a resident build on the deployed app and wait for its
        ``BuildSummary``."""
        build_function = modal.Function.from_name(app_name=self.name, name="build")
        return build_function.remote(
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
        settings: typing.Mapping[str, str] | None = None,
    ) -> BuildTriggerResult:
        """Trigger a build with a registry build id minted at the trigger.

        The build is created (or, with ``build_id``, resumed) in the registry
        from this process, then the deployed function driving it is spawned
        with that id. A restart of the driver resumes the same build: its
        plan for the same scope is reused, completed targets are observed,
        failed members reset.

        Args:
            tasks: The root task(s) — the build's request. A re-trigger names
                the same roots (a build is one request; other roots are
                refused — start a new build).
            worker_selector: Override the app's selector (resident only).
            build_kwargs: Forwarded to the resident ``build`` function
                (``stardag.build`` kwargs); not with ``reactive=True``.
            build_id: Resume this build instead of creating one.
            description: A description for a new build.
            reactive: Schedule the build with short-lived ticks instead of a
                resident orchestrator: the ``bootstrap`` function plans the
                build under its deployment, then ticks — spawned by the
                bootstrap, by workers finishing tasks and by the optional
                watchdog — drive it.
            tick_kwargs: ``TickConfig`` options stored with the build, shared
                by every tick of it.
            settings: Environment variables applied in every process of the
                build (bootstrap, ticks, workers, the resident driver). They
                are the second half of the build's scope: they may change
                structure and execution, never output. ``STARDAG_*`` and
                ``MODAL_*`` keys are refused. Omitted on a re-trigger
                (``build_id``), the build's active plan's settings are
                reused; ``{}`` explicitly means none.

        Returns:
            BuildTriggerResult with the build id and the spawned call.
        """
        checked_settings = validate_settings(settings)
        merged_kwargs = dict(build_kwargs or {})
        for reserved in ("resume_build_id", "settings"):
            if reserved in merged_kwargs:
                raise TypeError(
                    f"build_kwargs must not contain {reserved!r}; pass it to "
                    "build_trigger directly"
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
                "reactive=True: later scheduler ticks always use the app's "
                "deployed worker_selector. Configure it on StardagApp instead."
            )
        if reactive:
            tick_kwargs = _validate_tick_kwargs(tick_kwargs)
            if not self.task_modules:
                raise TaskModulesError(
                    "build_trigger(reactive=True) needs task_modules, and "
                    f"this app ({self.name!r}) has none. A reactive "
                    "scheduler tick reconstructs every task from registry "
                    "data and can resolve only classes whose modules it "
                    'imported. Pass task_modules=["my_pkg.tasks.*"] to '
                    "StardagApp, or use a resident build."
                )

        registry = registry_provider.get()
        if (build_id is None or reactive) and is_noop_registry(registry):
            raise RuntimeError(
                "build_trigger requires a configured registry to mint the "
                "build id at the trigger point (run 'stardag auth login' "
                "or configure an API key). Use build_spawn to trigger a "
                "build without local registry credentials."
            )
        task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
        # A bare re-trigger runs under the settings its build already has
        # (the active plan's), not under none; ``settings={}`` says "none"
        # explicitly. Without a registry here, the driver resolves it.
        settings_known = settings is not None
        if build_id is not None and settings is None and not is_noop_registry(registry):
            checked_settings = resolve_settings(registry, build_id, None)
            settings_known = True
        executor_metadata = self._build_executor_metadata(reactive=reactive)
        if build_id is None:
            build_id = registry.build_create(
                root_task_ids=[str(t.id) for t in task_list],
                build_id=new_id(),
                description=description,
                executor_metadata=executor_metadata,
            ).id
        elif not is_noop_registry(registry):
            # Un-terminal a re-triggered build (a no-op on a running one).
            # Deliberately outside the failure guard below: until this
            # succeeds the build may still be terminal, and failing it on
            # behalf of a resume that never landed would misattribute
            # someone else's outcome. The driver names its scope later.
            registry.build_resume(build_id, executor_metadata=executor_metadata)

        if reactive:
            return self._trigger_reactive(
                task_list,
                build_id=build_id,
                registry=registry,
                tick_kwargs=tick_kwargs,
                settings=checked_settings,
            )
        merged_kwargs["resume_build_id"] = build_id
        if settings_known:
            merged_kwargs["settings"] = checked_settings
        build_function = modal.Function.from_name(app_name=self.name, name="build")
        function_call = build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=merged_kwargs,
        )
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def _build_executor_metadata(self, *, reactive: bool) -> dict[str, typing.Any]:
        """Build-level executor metadata for a trigger (best-effort).

        ``function_name`` is the function the trigger spawns — ``bootstrap``
        for a reactive build discovered in Modal — so a reader looks in the
        right function's logs for a build that never started.
        """
        if not reactive:
            spawned = "build"
        elif self.reactive_discovery == "local":
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
                "Failed to resolve Modal workspace/environment for build "
                "executor metadata",
                exc_info=True,
            )
        return metadata

    def _trigger_reactive(
        self,
        task_list: list[BaseTask],
        *,
        build_id: UUID,
        registry: RegistryABC,
        tick_kwargs: dict[str, typing.Any] | None,
        settings: dict[str, str],
    ) -> BuildTriggerResult:
        """Spawn the ``bootstrap`` with the roots by value (or, with
        ``reactive_discovery="local"``, run it here: the plan is then made
        under the app's current deployment, D13).

        **No orphan RUNNING builds.** The build is RUNNING from here on, and
        any failure before the bootstrap is airborne records BUILD_FAILED
        before propagating; failures after it are the bootstrap's to report.
        """
        try:
            if self.reactive_discovery == "local":
                return BuildTriggerResult(
                    build_id=build_id,
                    function_call=run_reactive_bootstrap(
                        build_id,
                        task_list,
                        registry=registry,
                        app_name=self.name,
                        tick_kwargs=tick_kwargs,
                        task_module_patterns=self.task_modules,
                        settings=settings,
                    ).tick_call,
                )
            _advise_uncovered_root_task_modules(task_list, self.task_modules)
            bootstrap_function = modal.Function.from_name(
                app_name=self.name, name="bootstrap"
            )
            function_call = bootstrap_function.spawn(
                build_id=str(build_id),
                tasks=task_list,
                tick_kwargs=tick_kwargs,
                settings=settings,
            )
        except BaseException as e:
            _fail_build_best_effort(registry, build_id, e)
            raise
        return BuildTriggerResult(build_id=build_id, function_call=function_call)
