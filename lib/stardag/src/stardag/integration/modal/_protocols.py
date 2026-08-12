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
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).
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
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).

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


def _callable_accepts_env_overrides(fn: typing.Callable[..., typing.Any]) -> bool:
    """Whether ``fn`` accepts an ``env_overrides`` argument.

    Used to stay backward-compatible with custom ``RunFunction`` implementations
    written against the older ``(task)``-only signature (before the optional
    ``env_overrides`` parameter was added).
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "env_overrides":
            return True
    return False
