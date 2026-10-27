"""The two callables a StardagApp deploys: the build function and the run function.

Declared as protocols so an app can supply anything call-compatible.
:class:`stardag.integration.modal.Builder` and
:class:`stardag.integration.modal.Runner` are the default implementations and
the recommended starting point (they add overridable ``setup``/``teardown``
hooks on top of the protocol).
"""

from __future__ import annotations

import inspect
import typing

from stardag import BaseTask, TaskStruct
from stardag.build import BuildSummary
from stardag.integration.modal._selector import WorkerSelector


class BuildFunction(typing.Protocol):
    """Protocol for the function registered as the Modal "build" function.

    This function is called remotely on Modal to orchestrate a DAG build.
    It receives one or more root tasks, a worker selector, the Modal app
    name, and an optional ``build_kwargs`` dict, then coordinates task
    execution across Modal worker functions.

    Args (of ``__call__``):
        tasks: A single root ``BaseTask`` or a sequence of root tasks.
        worker_selector: Function picking a worker name per task.
        app_name: Name of the Modal app hosting the worker functions.
        build_kwargs: Optional dict of extra kwargs forwarded to the
            underlying build engine (the default ``Builder`` splats them
            into :func:`stardag.build`). ``None`` means "no extra kwargs".

    The default implementation (``Builder``) creates a ``ModalTaskExecutor``
    and calls ``stardag.build()``. Custom implementations can subclass
    ``Builder`` to override ``setup()``/``teardown()``/``build()``, or
    implement this protocol directly for full control.

    Any module-level code in the module where a custom build function is
    defined will execute inside the ``build`` container before the function
    is called: unpickling the serialized wrapper there reaches the
    callable's defining module — the function itself for a plain function,
    its class for a callable instance — and imports it. That covers the
    ``build`` container only; for setup every container of the app needs —
    workers and the reactive functions included — pass
    ``StardagApp(container_setup=...)``.

    That import is also why the module has to be one the container *has*:
    define a custom build function in your own package and import it into
    the file you deploy, never directly in that file (see
    ``StardagApp(container_setup=...)`` for the rule and the error).

    Implementations *may* additionally accept an optional keyword argument
    ``worker_timeouts: dict[str, int] | None``, as the default ``Builder``
    does. When the parameter is declared by name (a bare ``**kwargs`` does
    not count), the deployed ``build`` function passes the per-worker Modal
    ``timeout`` from the app's ``worker_settings``. Forward it to
    ``ModalTaskExecutor(worker_timeouts=...)``: it sets the claim TTL of
    each detached execution (the worker's timeout plus a grace period).
    Without it, a detached execution claims with the registry's default
    TTL (one hour), and a task running longer can be taken over by another
    build while it is still running.
    """

    def __call__(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None: ...


class RunFunction(typing.Protocol):
    """Protocol for the function registered as Modal "worker_*" functions.

    This function is called remotely on Modal to execute a single task.
    It receives a task instance and returns either ``None`` (task completed)
    or the ``TaskStruct`` yielded at the first incomplete dynamic-deps yield
    (which may include deps that are already complete — the caller is
    expected to filter if that matters). This enables idempotent
    re-execution: the build system schedules the yielded deps, then
    re-invokes the task. On re-execution the generator advances past
    previously-yielded batches whose deps are now complete.

    The default implementation (``Runner``) handles sync, async, and dynamic
    deps tasks. Custom implementations can subclass ``Runner`` to override
    ``setup()``/``teardown()``/``run()``, or implement this protocol directly.

    Any module-level code in the module where a custom run function is
    defined will execute inside every ``worker_*`` container before the
    function is called, by the same mechanism as ``BuildFunction`` above —
    use it for worker-specific setup. For setup every container of the app
    needs, pass ``StardagApp(container_setup=...)``. The same placement
    rule applies: define it in an importable module of your own package,
    not in the file you deploy.

    Args (of ``__call__``):
        task: The task instance to execute.

    Implementations *may* additionally accept an optional
    ``env_overrides: dict[str, str] | None`` keyword argument (the framework
    always forwards it by keyword). When the ``worker_selector`` returns
    ``(worker_name, env_overrides)`` (see :data:`WorkerSelection`), the
    framework forwards those overrides to run functions that accept the
    parameter; for run functions written against the older ``(task)``-only
    signature the framework instead applies the overrides to the process
    environment around the call. The default :class:`Runner` accepts
    ``env_overrides`` and applies them around its ``run`` call.

    **Lifecycle reporting.** In a detached execution somebody has to report
    the task's start, completion, failure and dynamic-dependency yields to
    the registry: either the worker itself, or the build engine on the
    worker's behalf. The default :class:`Runner` reports from the worker
    (unless constructed with ``report_lifecycle=False``). A run function
    declares which applies through a ``reports_lifecycle`` attribute, read
    once at ``finalize()``: true means it reports itself, so the engine
    stays silent; absent or false means the engine reports for it. A custom
    implementation that does not subclass ``Runner`` therefore gets
    engine-side reporting automatically, and should set
    ``reports_lifecycle = True`` only if it really reports (by delegating
    to a ``Runner``, say). Reactive builds need a self-reporting run
    function, since no resident engine is there to report:
    ``build_trigger(reactive=True)`` refuses an app whose run function does
    not report.

    **The worker rule.** A reactive tick may end a claim whose spawn never
    recorded an executor ref, and re-drive its task; that is safe only if a
    worker whose start went unrecorded does not run the task without its
    ref on the ledger. ``Runner`` keeps that rule and says so with
    ``keeps_worker_rule = True``. A custom run function should declare it
    only if it delegates to a ``Runner``; without it, such claims are left
    to their TTL.
    """

    def __call__(self, task: BaseTask) -> None | TaskStruct: ...


class _RunFunctionWithEnv(typing.Protocol):
    """Internal protocol for run functions that accept ``env_overrides``.

    Used to type the call site that forwards selector-provided environment
    overrides (see :meth:`StardagApp.finalize`'s ``_modal_run`` wrapper).
    """

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct: ...


def _run_function_keeps_worker_rule(run_function: typing.Any) -> bool:
    """Whether ``run_function`` keeps the worker rule of reconciliation:
    a reactive execution whose start went unrecorded does not run its task
    unless its own ref is on the ledger (``Runner`` does, see
    ``_reporter._require_ref_recorded``). Declared by a ``keeps_worker_rule``
    attribute, true on ``Runner``; a custom run function that delegates to
    a ``Runner`` may declare it too. Without it, a reactive tick does not
    end claims whose spawn never recorded a ref (STA-94/STA-135)."""
    return bool(getattr(run_function, "keeps_worker_rule", False)) and bool(
        getattr(run_function, "report_lifecycle", True)
    )


def _run_function_reports_lifecycle(run_function: typing.Any) -> bool:
    """Whether ``run_function`` reports the task lifecycle from the worker
    (see "Lifecycle reporting" on :class:`RunFunction`)."""
    return bool(getattr(run_function, "reports_lifecycle", False))


def _callable_accepts_env_overrides(fn: typing.Callable[..., typing.Any]) -> bool:
    """Whether ``fn`` accepts an ``env_overrides`` argument.

    Used to stay backward-compatible with custom ``RunFunction`` implementations
    written against the older ``(task)``-only signature (before the optional
    ``env_overrides`` parameter was added).
    """
    return _callable_accepts_keyword(fn, "env_overrides", var_keyword=True)


def _callable_accepts_keyword(
    fn: typing.Callable[..., typing.Any], name: str, *, var_keyword: bool
) -> bool:
    """Whether ``fn`` accepts a keyword argument ``name``.

    ``var_keyword`` decides whether a bare ``**kwargs`` counts. It should
    not when the callable is likely to splat its kwargs somewhere that
    would reject the extra one (a custom build function forwarding them to
    :func:`stardag.build`, say).
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if var_keyword and param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False
