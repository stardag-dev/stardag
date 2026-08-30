"""The deployed app's ``limit_key_selector``, published for worker-side code.

A task's named concurrency-limit keys are computed by a deployed-app
callable. Ticks get it through :class:`~stardag.integration.modal._tick._TickDeployment`;
the bootstrap gets it as an argument. The one place that needs it and is
nowhere near either is a **worker** registering dynamically yielded
dependencies (:meth:`_WorkerLifecycleReporter._register_dynamic_deps`),
which runs inside a worker container with only the task in hand. The
deployed worker wrapper publishes the selector here at container start, the
same way it publishes the app's task-module patterns.

Why keys are registered at plan time at all: the registry can only wake the
builds *queued on* a concurrency-limit key when a slot frees if it knows
which pending tasks want that key — and keys are a property of the task, so
registration is where it learns them. See ``services/wakeups.py`` in the
registry API.
"""

from __future__ import annotations

import typing

from stardag import BaseTask

LimitKeySelector = typing.Callable[[BaseTask], typing.Sequence[str]]
"""Maps a task to the named registry concurrency-limit keys it runs under."""

_deployed_selector: LimitKeySelector | None = None


def set_deployed_limit_key_selector(selector: LimitKeySelector | None) -> None:
    """Publish the app's selector for this process (called by the worker wrapper)."""
    global _deployed_selector
    _deployed_selector = selector


def deployed_limit_key_selector() -> LimitKeySelector | None:
    """The selector the worker wrapper published, or None outside a deployed worker."""
    return _deployed_selector
