"""Declaring the modules whose import registers a build's task classes.

Reactive scheduling reconstructs task *objects* from data, and the two
available paths have disjoint failure modes:

======================================  ====================================
path                                    fails when
======================================  ====================================
pickle from the ``BuildTaskStore``      the app was redeployed (pickles are
                                        same-deployment only), or the target
                                        root is not writable
``task_from_registry_data``             the module defining the task class
                                        was never imported in the
                                        reconstructing process
======================================  ====================================

The second failure mode is easy to miss. A pickle embeds
``module.QualName`` and ``pickle.loads`` **self-imports**, so it resolves
its class regardless of what the container happened to import. Polymorphic
JSON carries only ``__namespace`` / ``__name``; ``get_class()`` is a plain
dict lookup that raises ``KeyError`` and **never attempts an import**.
Since the default namespace is ``""`` (the module path is consulted only to
resolve an explicitly registered namespace), the stored payload generally
contains no module locator at all. Task classes register at *class
definition* time, so the only way to make a class resolvable is to import
its defining module.

Meanwhile the deployed scheduler tick is defined inside stardag itself and
drags in user modules only incidentally — whatever the app's selector
callables transitively import. That is arbitrary, and usually does not
cover a DAG's task classes: DAGs are typically assembled in scripts and
entrypoints rather than in the module defining the app.

Hence this module: a way for an app to *declare* the modules whose import
registers the task classes it may schedule, so a scheduler process can make
itself able to reconstruct them. Nothing here is Modal-specific, and
nothing here is needed by a resident (non-reactive) build — a resident
orchestrator holds the real task objects, and workers receive tasks by
value (self-importing, like any unpickle).

Three pieces, used at three different times:

1. **Deploy time** — :func:`expand_task_module_patterns` turns the declared
   patterns into a concrete, sorted module list *without importing the
   submodules*, so the deployed set is explicit and auditable and container
   startup does no filesystem walking.
2. **Container startup** — :func:`import_task_modules` imports that baked
   list once per container. A module that fails to import warns rather than
   aborting the tick, but the failure is retained
   (:func:`last_import_failures`) so a later rehydration error can point at
   it.
3. **Trigger time** — :func:`uncovered_task_classes` and
   :func:`plan_pickle_elision` answer "will a tick be able to rebuild this
   task from registry data alone?" for a concrete task set, which is what
   lets the trigger skip writing pickles (and warn about the classes it
   cannot skip).

Pattern grammar
---------------

A pattern is either an exact module (``"a.b.c"``) or a trailing recursive
wildcard (``"a.b.*"``, matching ``a.b`` and everything below it). A ``*``
anywhere but the final component, an empty pattern, or a component that is
not a valid identifier is a :class:`TaskModulesError` — a malformed pattern
must never degrade into a silent no-match, because the symptom would be a
scheduler tick failing to rebuild a task hours later.

Two deliberate expansion choices, both about what a wildcard sweeps up:

- ``__main__`` submodules are **skipped**. They are CLI entrypoints, run
  for their side effects; importing one in every scheduler container is at
  best wasted work and at worst an unwanted execution.
- ``_``-prefixed modules are **not** skipped. A user's ``_tasks.py`` is a
  perfectly ordinary place to define task classes, and the leading
  underscore is a statement about *their* API, not about ours.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import typing
from dataclasses import dataclass, field

from stardag._core.base_task import BaseTask
from stardag._core.rehydrate import task_from_registry_data
from stardag.exceptions import StardagError

logger = logging.getLogger(__name__)

_WILDCARD_SUFFIX = ".*"


class TaskModulesError(StardagError):
    """A task-module declaration is malformed, unexpandable, or unsatisfied."""


# =============================================================================
# Patterns: validation, expansion, coverage
# =============================================================================


def validate_task_module_patterns(
    patterns: typing.Iterable[str],
) -> tuple[str, ...]:
    """Validate task-module patterns; return them deduped and sorted.

    Raises:
        TaskModulesError: For anything that is not an exact dotted module
            path or such a path followed by a trailing ``.*``.
    """
    validated: set[str] = set()
    for pattern in patterns:
        if not isinstance(pattern, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TaskModulesError(
                f"task_modules entries must be strings, got {pattern!r} "
                f"({type(pattern).__name__})."
            )
        _validate_one(pattern)
        validated.add(pattern)
    return tuple(sorted(validated))


def _validate_one(pattern: str) -> None:
    problem = _pattern_problem(pattern)
    if problem is None:
        return
    raise TaskModulesError(
        f"Invalid task_modules pattern {pattern!r}: {problem}. A pattern is "
        'either an exact module ("my_pkg.tasks.ingest") or a package '
        'followed by a trailing recursive wildcard ("my_pkg.tasks.*").'
    )


def _pattern_problem(pattern: str) -> str | None:
    if not pattern or pattern.strip() != pattern:
        return "it is empty or has surrounding whitespace"
    if pattern in ("*", _WILDCARD_SUFFIX):
        return (
            "a bare wildcard would match every importable module; name at "
            "least the root package"
        )
    components = pattern.split(".")
    if pattern.endswith(_WILDCARD_SUFFIX):
        components = components[:-1]
    for component in components:
        if component == "*":
            return "'*' is only allowed as the final component"
        if not component.isidentifier():
            return f"component {component!r} is not a valid Python identifier"
    return None


def module_is_covered(module_name: str, patterns: typing.Sequence[str]) -> bool:
    """Whether importing the declared ``patterns`` reaches ``module_name``.

    Mirrors :func:`expand_task_module_patterns`, including its ``__main__``
    exclusion — a class defined in a ``__main__`` module is never
    reconstructable in a scheduler container, wildcard or not.
    """
    if module_name == "__main__" or module_name.endswith(".__main__"):
        return False
    for pattern in patterns:
        if pattern.endswith(_WILDCARD_SUFFIX):
            root = pattern[: -len(_WILDCARD_SUFFIX)]
            if module_name == root or module_name.startswith(root + "."):
                return True
        elif module_name == pattern:
            return True
    return False


def suggested_pattern_for(module_name: str) -> str:
    """The narrowest pattern that would cover ``module_name``.

    The module's own package plus a recursive wildcard (or the module
    itself when it is top-level) — precise enough to paste into
    ``task_modules`` without dragging in half the source tree.
    """
    package, _, _ = module_name.rpartition(".")
    return f"{package}{_WILDCARD_SUFFIX}" if package else module_name


def expand_task_module_patterns(
    patterns: typing.Iterable[str],
) -> list[str]:
    """Expand patterns to the concrete, sorted, deduped module list.

    **Importing is avoided wherever it can be.** ``pkgutil.walk_packages``
    is deliberately not used: it imports each package to reach its
    ``__path__``, which would run every ``__init__.py`` in the tree just to
    *list* names. Only the root package of a ``"pkg.*"`` pattern is
    imported (unavoidable — its ``__path__`` is the entry point to the
    tree); everything below it is discovered from the filesystem with
    ``pkgutil.iter_modules``.

    Raises:
        TaskModulesError: If a pattern is malformed, or the root package of
            a wildcard pattern cannot be imported (a typo'd pattern must
            fail loudly at deploy time rather than expand to nothing).
    """
    modules: set[str] = set()
    for pattern in validate_task_module_patterns(patterns):
        if not pattern.endswith(_WILDCARD_SUFFIX):
            modules.add(pattern)
            continue
        root = pattern[: -len(_WILDCARD_SUFFIX)]
        modules.add(root)
        modules.update(_iter_submodules(root, pattern))
    return sorted(modules)


def _iter_submodules(root: str, pattern: str) -> typing.Iterator[str]:
    try:
        package = importlib.import_module(root)
    except Exception as e:
        raise TaskModulesError(
            f"Cannot expand task_modules pattern {pattern!r}: the root "
            f"package {root!r} could not be imported ({type(e).__name__}: "
            f"{e}). Expansion needs the package's __path__; check the "
            "pattern for typos and make sure the package is importable "
            "from the process running the deploy."
        ) from e
    search_paths = list(getattr(package, "__path__", []))
    if not search_paths:
        # A plain module, not a package: the wildcard has nothing below it
        # to recurse into. The root itself is already included by the
        # caller, so this is a harmless (if pointless) pattern.
        return
    # Guard against symlink loops in the source tree: a directory is walked
    # at most once, by its resolved path.
    seen_dirs: set[str] = set()
    stack: list[tuple[str, list[str]]] = [(root, search_paths)]
    while stack:
        prefix, paths = stack.pop()
        for path in paths:
            real = os.path.realpath(path)
            if real in seen_dirs:
                continue
            seen_dirs.add(real)
            for module_info in pkgutil.iter_modules([path]):
                if module_info.name == "__main__":
                    # Entrypoints: usually side-effectful, never a place to
                    # define task classes (see the module docstring).
                    continue
                qualified = f"{prefix}.{module_info.name}"
                yield qualified
                if module_info.ispkg:
                    stack.append((qualified, [os.path.join(path, module_info.name)]))


# =============================================================================
# Importing (in the reconstructing process)
# =============================================================================


@dataclass(frozen=True)
class TaskModuleImportReport:
    """Outcome of :func:`import_task_modules`."""

    imported: tuple[str, ...] = ()
    # module name -> "ExceptionType: message"
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def task_classes_registered(self) -> int:
        """Task classes registered from the imported modules.

        Counted after the fact from the polymorphic registry rather than as
        a delta, so it is stable when some modules were already imported.
        """
        return count_registered_task_classes(self.imported)


_import_cache: dict[tuple[str, ...], TaskModuleImportReport] = {}
_last_failures: dict[str, str] = {}


def import_task_modules(
    modules: typing.Sequence[str],
) -> TaskModuleImportReport:
    """Import ``modules`` so their task classes register; never raise.

    Idempotent and cached on the exact module list: a container that serves
    many scheduler ticks pays the walk once, and re-importing an
    already-imported module is a ``sys.modules`` hit anyway.

    A module that fails to import is **warned about, not fatal** — one bad
    module (a missing optional dependency, a syntax error in a module
    nobody schedules) must not take the whole scheduler down. The failures
    are retained in the report and in :func:`last_import_failures` so that
    a downstream "no task class registered for …" error can name the likely
    cause instead of leaving the user to guess.
    """
    global _last_failures
    key = tuple(modules)
    cached = _import_cache.get(key)
    if cached is not None:
        _last_failures = dict(cached.failures)
        return cached

    imported: list[str] = []
    failures: dict[str, str] = {}
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as e:
            failures[module] = f"{type(e).__name__}: {e}"
            logger.warning(
                f"Declared task module {module!r} failed to import: "
                f"{type(e).__name__}: {e}. Task classes defined there will "
                "not be reconstructable from registry data."
            )
            continue
        imported.append(module)
    report = TaskModuleImportReport(imported=tuple(imported), failures=failures)
    _import_cache[key] = report
    _last_failures = dict(failures)
    logger.info(
        f"Imported {len(imported)}/{len(key)} declared task module(s)"
        + (f"; {len(failures)} failed: {sorted(failures)}" if failures else ".")
    )
    return report


def last_import_failures() -> dict[str, str]:
    """Task modules that failed to import in this process, most recent call.

    Diagnostics hook: a ``TaskRehydrationError`` naming an unresolved class
    is far more actionable when it can add "…and by the way, these declared
    task modules failed to import".
    """
    return dict(_last_failures)


def import_failure_note(max_listed: int = 5) -> str:
    """A one-line addendum naming import failures, or ``""`` if there were none."""
    failures = _last_failures
    if not failures:
        return ""
    listed = sorted(failures)[:max_listed]
    rendered = "; ".join(f"{name} ({failures[name]})" for name in listed)
    more = len(failures) - len(listed)
    return (
        f" Note: {len(failures)} declared task module(s) failed to import in "
        f"this process, which is a likely cause: {rendered}"
        + (f" (+{more} more)" if more else "")
        + "."
    )


def count_registered_task_classes(modules: typing.Iterable[str]) -> int:
    """How many registered task classes are defined in ``modules``."""
    wanted = set(modules)
    return sum(1 for cls in BaseTask._registry().classes() if cls.__module__ in wanted)


def _reset_import_state_for_tests() -> None:
    """Reset **all** module-level state (tests only).

    Every piece of ambient state this module keeps, not just the import
    cache: a declaration or a one-shot warning surviving into the next test
    makes results depend on execution order, and `only_unwarned=True` in
    particular is silently order-sensitive.
    """
    _import_cache.clear()
    _last_failures.clear()
    _warned_classes.clear()
    global _declared_patterns
    _declared_patterns = ()


# =============================================================================
# The ambient declaration (for code far from the app object)
# =============================================================================

_declared_patterns: tuple[str, ...] = ()


def set_declared_task_module_patterns(patterns: typing.Sequence[str]) -> None:
    """Record the executing app's task-module patterns for this process.

    Deployed worker/scheduler entrypoints bake the app's patterns into
    their closure and publish them here, because the code that needs them
    — dynamic-dependency registration inside a worker, for instance — sits
    several frames below any reference to the app object.
    """
    global _declared_patterns
    # Normalised, not stored verbatim: these patterns drive coverage checks
    # and the pickle-elision decision, and a stray space makes a pattern
    # match nothing while still reading as a declaration at the call site.
    # A silently-inert declaration is the worst outcome here — it turns
    # "ticks reconstruct tasks by import" back into "ticks need the
    # pickles" with no signal.
    cleaned = tuple(p.strip() for p in patterns)
    if any(not p for p in cleaned):
        raise ValueError(
            f"task-module patterns must be non-empty: {list(patterns)!r}. "
            "An empty or whitespace-only pattern matches no module, so the "
            "declaration would be silently inert."
        )
    _declared_patterns = cleaned


def declared_task_module_patterns() -> tuple[str, ...]:
    """The patterns published by :func:`set_declared_task_module_patterns`."""
    return _declared_patterns


# =============================================================================
# Coverage of a concrete task set
# =============================================================================

_warned_classes: set[str] = set()


def uncovered_task_classes(
    tasks: typing.Iterable[BaseTask],
    patterns: typing.Sequence[str],
    *,
    only_unwarned: bool = False,
) -> list[type[BaseTask]]:
    """Distinct classes in ``tasks`` that ``patterns`` does not reach.

    Args:
        tasks: The tasks to check. At trigger time this should be the
            *incomplete* discovered set — the only tasks a tick ever
            rehydrates.
        patterns: The app's declared task-module patterns.
        only_unwarned: Report each class at most once per process, and
            record the ones reported. For hot paths (every worker
            registering dynamic deps) where the same class would otherwise
            produce the same warning on every invocation.
    """
    seen: dict[str, type[BaseTask]] = {}
    for task in tasks:
        cls = type(task)
        key = f"{cls.__module__}.{cls.__qualname__}"
        if key in seen or module_is_covered(cls.__module__, patterns):
            continue
        if only_unwarned:
            if key in _warned_classes:
                continue
            _warned_classes.add(key)
        seen[key] = cls
    return [seen[key] for key in sorted(seen)]


def format_uncovered_message(
    uncovered: typing.Sequence[type[BaseTask]],
    patterns: typing.Sequence[str],
    *,
    remedy: str = "",
) -> str:
    """Render the actionable "these classes are not covered" message."""
    names = [f"{cls.__module__}.{cls.__qualname__}" for cls in uncovered]
    suggestions = sorted({suggested_pattern_for(cls.__module__) for cls in uncovered})
    head = (
        f"Task class {names[0]} is"
        if len(names) == 1
        else f"{len(names)} task classes ({', '.join(names)}) are"
    )
    declared = list(patterns) if patterns else "not declared"
    return (
        f"{head} not covered by this app's task_modules ({declared}). A "
        "reactive scheduler tick will not be able to reconstruct them from "
        "registry data, so they stay dependent on the build task store's "
        f"pickles. Add {suggestions} to task_modules and redeploy the app."
        + (f" {remedy}" if remedy else "")
    )


# =============================================================================
# Conditional pickle elision
# =============================================================================


@dataclass(frozen=True)
class PickleElisionPlan:
    """Which tasks still need a pickle in the build task store, and why.

    A task needs no pickle when its class is covered by the declared
    patterns *and* its registration payload round-trips back to the same
    task id. Everything else keeps the pickle it would have gotten anyway,
    so the feature is purely subtractive — nothing that works today stops
    working because a task did not qualify.
    """

    pickle_free: tuple[BaseTask, ...] = ()
    # (task, reason) for the tasks that must still be pickled
    pickled: tuple[tuple[BaseTask, str], ...] = ()

    def summary(self) -> str:
        """One-line log summary: counts, plus the distinct reasons."""
        line = (
            f"{len(self.pickle_free)} task(s) pickle-free, {len(self.pickled)} pickled"
        )
        if not self.pickled:
            return line + "."
        reasons: dict[str, int] = {}
        for _, reason in self.pickled:
            reasons[reason] = reasons.get(reason, 0) + 1
        rendered = "; ".join(
            f"{reason} (x{count})" if count > 1 else reason
            for reason, count in sorted(reasons.items())
        )
        return f"{line} — {rendered}."

    def require_pickle_free_error(self) -> str | None:
        """Message for ``require_pickle_free``, or None if everything qualified."""
        if not self.pickled:
            return None
        lines = [
            f"  - {type(task).__module__}.{type(task).__qualname__} "
            f"(task {task.id}): {reason}"
            for task, reason in self.pickled
        ]
        return (
            f"require_pickle_free=True, but {len(self.pickled)} task(s) "
            "cannot be reconstructed from registry data alone and would "
            "need a pickle in the build task store:\n" + "\n".join(lines)
        )


_UNCOVERED_REASON = "task class not covered by task_modules"


def plan_pickle_elision(
    tasks: typing.Iterable[BaseTask],
    patterns: typing.Sequence[str],
) -> PickleElisionPlan:
    """Decide, per task, whether the build task store still needs its pickle.

    The self-check reconstructs the task from exactly the payload that
    registration stores (``model_dump(mode="json")`` — see
    ``_get_task_data_for_registration``), so it is a faithful dry run of
    what a scheduler tick will do. Note this is *cheaper* than the write it
    replaces: pure CPU, versus a per-task target-root existence check plus
    a conditional upload.

    ``AliasTask`` payloads fail the check by construction (rehydration
    refuses ``__aliased`` data, whose pickled ``loads_type`` would be an
    execution primitive in a scheduler process), as do dynamically
    generated and otherwise non-importable classes. Those are exactly the
    cases that keep their pickles.
    """
    pickle_free: list[BaseTask] = []
    pickled: list[tuple[BaseTask, str]] = []
    for task in tasks:
        if not module_is_covered(type(task).__module__, patterns):
            pickled.append((task, _UNCOVERED_REASON))
            continue
        try:
            task_from_registry_data(
                task.model_dump(mode="json"), expected_task_id=task.id
            )
        except Exception as e:
            pickled.append((task, f"registry-data round-trip failed ({e})"))
            continue
        pickle_free.append(task)
    return PickleElisionPlan(pickle_free=tuple(pickle_free), pickled=tuple(pickled))
