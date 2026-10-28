"""Making the task classes a stored body names importable, without the user
importing every one of them by hand.

A stored body (from the registry or the committed task store) names its
classes by ``__namespace`` / ``__name`` only, and a namespace is an
**identity, not an import location**: nothing here ever reads one as a
module path. Two sources of modules instead:

1. **Module hints** (:func:`import_module_hints`): the task store records,
   per stored body, the ``__module__`` of the live class that wrote it. The
   store reader imports the hint of every class not yet registered before
   validating. A hint that fails to import (the module moved) is not fatal:
   the next source, or the final error, takes over.
2. **The package's ``task_modules``** (:func:`import_project_task_modules`):
   ``[tool.stardag] task_modules`` in the ``pyproject.toml`` that applies to
   the call (:mod:`stardag._core.anchor`), in the grammar of
   ``StardagApp(task_modules=...)``. Imported once per process and pattern
   set, lazily: the first time rehydration misses a class
   (:func:`stardag._core.rehydrate.task_from_body`, ``from_registry``, a
   ``TaskRef`` hydration, ``from_store``), after which the lookup is retried
   once.

No lock is held while user modules import: a module whose import itself
rehydrates (in the importing thread or one it waits for) cannot deadlock on
it. The importing thread's own nested misses do not re-import; another
thread missing meanwhile waits for the import, bounded by
``_WAIT_SECONDS``, then retries either way.
"""

from __future__ import annotations

import importlib
import logging
import threading
import typing

from stardag._core.base_task import BaseTask
from stardag.polymorphic import TypeId

logger = logging.getLogger(__name__)

_WAIT_SECONDS = 60.0
_LOCK = threading.Lock()
# Per pattern set: done, or in progress (an Event set when done, and the
# importing thread's id).
_done: set[tuple[str, ...]] = set()
_in_progress: dict[tuple[str, ...], tuple[threading.Event, int]] = {}
_expansion_errors: dict[tuple[str, ...], str] = {}
_hint_failures: dict[str, str] = {}


def is_registered(namespace: str, name: str) -> bool:
    """Whether a task class is registered under ``namespace`` / ``name``."""
    try:
        BaseTask._registry().get_class(TypeId(namespace=namespace, name=name))
    except KeyError:
        return False
    return True


def import_module_hints(hints: typing.Iterable[tuple[str, str, str | None]]) -> None:
    """Import the module hint of every ``(namespace, name, module)`` whose
    class is not registered yet. Best effort: a failure is logged and kept
    for :func:`import_note`, never raised."""
    for namespace, name, module in hints:
        if not module or is_registered(namespace, name):
            continue
        try:
            importlib.import_module(module)
        except Exception as e:
            with _LOCK:
                _hint_failures[module] = f"{type(e).__name__}: {e}"
            logger.debug("Module hint %r failed to import: %s", module, e)


def project_task_module_patterns() -> tuple[str, ...]:
    """The ``[tool.stardag] task_modules`` patterns that apply to the
    current call (see :func:`stardag._core.anchor.call_anchors`)."""
    from stardag._core.anchor import call_anchors
    from stardag.config.project import ProjectConfigError, resolve_project_config

    try:
        return resolve_project_config(call_anchors()).task_modules
    except ProjectConfigError as e:
        logger.warning("Package settings could not be read: %s", e)
        return ()


def import_project_task_modules() -> bool:
    """Import the applicable ``task_modules``, once per process and pattern
    set. Whether they are imported now, i.e. whether a lookup that just
    missed is worth retrying. Never raises: an unexpandable pattern is kept
    for :func:`import_note`."""
    patterns = project_task_module_patterns()
    if not patterns:
        return False
    me = threading.get_ident()
    event = threading.Event()
    with _LOCK:
        if patterns in _done:
            return True
        running = _in_progress.get(patterns)
        if running is None:
            _in_progress[patterns] = (event, me)
    if running is not None:
        event, owner = running
        if owner == me:
            return False  # a module being imported rehydrates: no recursion
        event.wait(_WAIT_SECONDS)
        return True
    try:
        return _import(patterns)
    finally:
        with _LOCK:
            _done.add(patterns)
            _in_progress.pop(patterns, None)
        event.set()


def _import(patterns: tuple[str, ...]) -> bool:
    from stardag.build._task_modules import (
        TaskModulesError,
        expand_task_module_patterns,
        import_task_modules,
    )

    try:
        modules = expand_task_module_patterns(patterns)
    except TaskModulesError as e:
        with _LOCK:
            _expansion_errors[patterns] = str(e)
        logger.warning("task_modules could not be expanded: %s", e)
        return False
    import_task_modules(modules)
    return True


def import_note() -> str:
    """An addendum for a "no task class registered" error: what was tried
    and what failed, or how to configure it."""
    from stardag.build._task_modules import import_failure_note

    patterns = project_task_module_patterns()
    if not patterns:
        note = (
            " To have such classes imported on demand, list the modules that "
            "define them under [tool.stardag] task_modules in the pyproject.toml "
            'of their package (e.g. task_modules = ["my_package.tasks.*"]).'
        )
    else:
        with _LOCK:
            expansion_error = _expansion_errors.get(patterns)
        if expansion_error:
            note = (
                f" The configured task_modules {list(patterns)} could not be "
                f"expanded: {expansion_error}"
            )
        else:
            note = (
                f" The configured task_modules {list(patterns)} were imported."
                + import_failure_note()
            )
    with _LOCK:
        hints = dict(_hint_failures)
    if hints:
        listed = "; ".join(f"{m} ({err})" for m, err in sorted(hints.items())[:5])
        note += f" Module hints that failed to import: {listed}."
    return note


def _reset_for_tests() -> None:
    with _LOCK:
        _done.clear()
        _in_progress.clear()
        _expansion_errors.clear()
        _hint_failures.clear()
