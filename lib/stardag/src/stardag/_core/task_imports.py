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
2. **The project's ``task_modules``** (:func:`import_project_task_modules`):
   patterns in ``[project] task_modules`` of ``.stardag/config.toml``, in
   the grammar of ``StardagApp(task_modules=...)``. Imported once per
   process, lazily: the first time rehydration misses a class
   (:func:`stardag._core.rehydrate.task_from_body`, ``from_registry``, a
   ``TaskRef`` hydration, ``from_store``), after which the lookup is retried
   once.
"""

from __future__ import annotations

import importlib
import logging
import threading
import typing

from stardag._core.base_task import BaseTask
from stardag.polymorphic import TypeId

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
# The patterns last imported, or None: imported at most once per distinct
# configuration in a process.
_imported_for: tuple[str, ...] | None = None
_expansion_error: str | None = None
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
    """The configured ``[project] task_modules`` patterns."""
    from stardag.config.project import get_project_config

    return get_project_config().task_modules


def import_project_task_modules() -> bool:
    """Import the project's ``task_modules``, once per process (per
    configuration). Whether this call imported them, i.e. whether a lookup
    that just missed is worth retrying. Never raises: an unexpandable
    pattern is kept for :func:`import_note`."""
    global _imported_for, _expansion_error
    patterns = project_task_module_patterns()
    if not patterns:
        return False
    with _LOCK:
        if _imported_for == patterns:
            return False
        _imported_for = patterns
    from stardag.build._task_modules import (
        TaskModulesError,
        expand_task_module_patterns,
        import_task_modules,
    )

    try:
        modules = expand_task_module_patterns(patterns)
    except TaskModulesError as e:
        with _LOCK:
            _expansion_error = str(e)
        logger.warning("Project task_modules could not be expanded: %s", e)
        return False
    with _LOCK:
        _expansion_error = None
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
            "define them under [project] task_modules in .stardag/config.toml "
            '(e.g. task_modules = ["my_project.tasks.*"]).'
        )
    else:
        note = f" The project's task_modules {list(patterns)} were imported."
        if _expansion_error:
            note = (
                f" The project's task_modules {list(patterns)} could not be "
                f"expanded: {_expansion_error}"
            )
        else:
            note += import_failure_note()
    with _LOCK:
        hints = dict(_hint_failures)
    if hints:
        listed = "; ".join(f"{m} ({err})" for m, err in sorted(hints.items())[:5])
        note += f" Module hints that failed to import: {listed}."
    return note


def _reset_for_tests() -> None:
    global _imported_for, _expansion_error
    with _LOCK:
        _imported_for = None
        _expansion_error = None
        _hint_failures.clear()
