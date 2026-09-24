"""Declaring the modules whose import registers a build's task classes.

Reactive scheduling reconstructs task *objects* from data: a scheduler
tick is a short-lived process that learns *which* tasks are actionable
from the registry frontier, and rebuilds each one from the identity-level
``task_data`` the registry stored at registration
(:func:`stardag.task_from_registry_data`). That is the **only**
representation of a task outside a running process — there is no pickle
store any more (see ``RELEASE_NOTES.md``).

Which makes one failure mode load-bearing: ``task_from_registry_data``
fails when the module defining the task class was never imported in the
reconstructing process. Polymorphic JSON carries only ``__namespace`` /
``__name``; ``get_class()`` is a plain dict lookup that raises
``KeyError`` and **never attempts an import**. Since the default namespace
is ``""`` (the module path is consulted only to resolve an explicitly
registered namespace), the stored payload generally contains no module
locator at all. Task classes register at *class definition* time, so the
only way to make a class resolvable is to import its defining module.

Meanwhile the deployed scheduler tick is defined inside stardag itself and
drags in user modules only incidentally — whatever the app's selector
callables transitively import. That is arbitrary, and usually does not
cover a DAG's task classes: DAGs are typically assembled in scripts and
entrypoints rather than in the module defining the app.

Hence this module: a way for an app to *declare* the modules whose import
registers the task classes it may schedule, so a scheduler process can
make itself able to reconstruct them. Nothing here is Modal-specific, and
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
   :func:`plan_rehydration` answer "will a tick be able to rebuild this
   task from registry data?" for a concrete task set. That is now a
   *precondition*, not a preference: the reactive bootstrap refuses to arm
   a build any of whose incomplete tasks fails it, naming each one.

Pattern grammar
---------------

A pattern is either an exact module (``"a.b.c"``) or a trailing recursive
wildcard (``"a.b.*"``, matching ``a.b`` and everything below it). A ``*``
anywhere but the final component, an empty pattern, or a component that is
not a valid identifier is a :class:`TaskModulesError` — a malformed pattern
must never degrade into a silent no-match, because the symptom would be a
build refused (or, for a dynamically yielded dependency, a task failed)
with a coverage message naming classes the user believes they declared.

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


def module_is_main(module_name: str) -> bool:
    """Whether ``module_name`` is a ``__main__`` module.

    Its own predicate because it is the one *unfixable* reason a class is
    not reconstructable, and the difference has to reach the user. Every
    other unreachable module is fixed by adding a pattern; this one is
    fixed only by moving the class. A remedy that cannot work is worse than
    none — ``my_pkg.__main__`` in particular would otherwise be told to add
    ``my_pkg.*``, which reads as entirely plausible and changes nothing.
    """
    return module_name == "__main__" or module_name.endswith(".__main__")


def module_is_covered(module_name: str, patterns: typing.Sequence[str]) -> bool:
    """Whether importing the declared ``patterns`` reaches ``module_name``.

    Mirrors :func:`expand_task_module_patterns`, including its ``__main__``
    exclusion — a class defined in a ``__main__`` module is never
    reconstructable in a scheduler container, wildcard or not. A caller
    that reports *why* must ask :func:`module_is_main` first; this one
    collapses the two cases into a single False.
    """
    if module_is_main(module_name):
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
    # Normalised, not stored verbatim: these patterns decide whether a
    # class is reconstructable at all, and a stray space makes a pattern
    # match nothing while still reading as a declaration at the call site.
    # A silently-inert declaration is the worst outcome here — every class
    # it was meant to cover becomes one a tick cannot rebuild, with no
    # signal until the warning or the refusal names it.
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
    head = (
        f"Task class {names[0]} is"
        if len(names) == 1
        else f"{len(names)} task classes ({', '.join(names)}) are"
    )
    declared = list(patterns) if patterns else "not declared"
    return (
        f"{head} not covered by this app's task_modules ({declared}). A "
        "reactive scheduler tick reconstructs every task it schedules from "
        "registry data, and can only do that for a class whose module it "
        "has imported."
        + _remedy_for(cls.__module__ for cls in uncovered)
        + (f" {remedy}" if remedy else "")
    )


def _remedy_for(modules: typing.Iterable[str]) -> str:
    """The fix for a set of unreachable modules, split by which fix applies.

    A ``__main__`` module gets its own sentence, because no pattern reaches
    one: :func:`module_is_covered` excludes it outright, so telling the
    user to add ``my_pkg.*`` for a class in ``my_pkg.__main__`` sends them
    through a redeploy to the identical message.
    """
    listed = list(modules)
    suggestions = sorted(
        {suggested_pattern_for(m) for m in listed if not module_is_main(m)}
    )
    main_modules = sorted({m for m in listed if module_is_main(m)})
    parts = []
    if suggestions:
        parts.append(f" Add {suggestions} to task_modules and redeploy the app.")
    if main_modules:
        parts.append(
            f" {main_modules} cannot be covered by any pattern — a __main__ "
            "module is an entrypoint, never importable under a stable name in "
            "a container. Move the task class into an ordinary module of your "
            "package and declare that module instead."
        )
    return "".join(parts)


# =============================================================================
# The rehydration pre-flight
# =============================================================================


@dataclass(frozen=True)
class RehydrationPlan:
    """Which tasks a scheduler tick could rebuild from registry data, and why not.

    A task is reconstructable when its class is covered by the declared
    patterns *and* its registration payload round-trips back to the same
    task id. Anything else is unschedulable in a reactive build: the tick
    that would put it on a worker has nothing to build the object from.
    """

    reconstructable: tuple[BaseTask, ...] = ()
    # (task, reason) for the tasks a tick could not rebuild
    unreconstructable: tuple[tuple[BaseTask, str], ...] = ()

    def summary(self) -> str:
        """One-line log summary: counts, plus the distinct reasons."""
        line = (
            f"{len(self.reconstructable)} task(s) reconstructable, "
            f"{len(self.unreconstructable)} not"
        )
        if not self.unreconstructable:
            return line + "."
        reasons: dict[str, int] = {}
        for _, reason in self.unreconstructable:
            reasons[reason] = reasons.get(reason, 0) + 1
        rendered = "; ".join(
            f"{reason} (x{count})" if count > 1 else reason
            for reason, count in sorted(reasons.items())
        )
        return f"{line} — {rendered}."

    def error(self, patterns: typing.Sequence[str]) -> str | None:
        """The refusal message, or None when every task qualified.

        **The listing is truncated**, and the unit it truncates to is the
        ``(class, reason)`` pair. The offending set is normally one bad
        class over every task of a fan-out, so an unbounded listing would
        be thousands of identical lines — in the log, and in the
        ``error_message`` the caller records on the build. One example task
        id per pair is what makes the failure diagnosable; the rest are the
        same fact repeated.

        **Not by class alone**, because a reason can be task-specific: two
        tasks of one class fail the round trip with different exception
        text, and collapsing them would pick one arbitrary reason and then
        claim the count applies to it. The coverage reasons are constants,
        so the common case is still a single line.
        """
        if not self.unreconstructable:
            return None
        example: dict[tuple[str, str], BaseTask] = {}
        counts: dict[tuple[str, str], int] = {}
        for task, reason in self.unreconstructable:
            cls = type(task)
            key = (f"{cls.__module__}.{cls.__qualname__}", reason)
            example.setdefault(key, task)
            counts[key] = counts.get(key, 0) + 1
        listed = sorted(example)[:_MAX_LISTED_GROUPS]
        lines = []
        for key in listed:
            class_name, reason = key
            task = example[key]
            others = counts[key] - 1
            more = f" (and {others} more task(s), same reason)" if others else ""
            lines.append(f"  - {class_name} (e.g. task {task.id}): {reason}{more}")
        hidden = len(example) - len(listed)
        if hidden:
            lines.append(f"  - ...and {hidden} further class/reason group(s).")
        # Deliberately NOT truncated with the listing: the remedy has to
        # cover every unreachable class, including the ones the listing
        # dropped, or following it leaves the build refused for the same
        # reason. It is short regardless — `suggested_pattern_for` collapses
        # a module to its package.
        #
        # Only the two *module reachability* reasons get a remedy. A
        # round-trip failure has no one-line fix, and its per-task line
        # already carries the exception.
        remedy = _remedy_for(
            type(task).__module__
            for task, reason in self.unreconstructable
            if reason in (_UNCOVERED_REASON, _MAIN_MODULE_REASON)
        )
        declared = list(patterns) if patterns else "not declared"
        return (
            f"{len(self.unreconstructable)} task(s) in this build cannot be "
            "reconstructed from registry data, so a reactive scheduler tick "
            "could never put them on a worker:\n"
            + "\n".join(lines)
            + f"\n\nThis app's task_modules: {declared}."
            + remedy
        )


_UNCOVERED_REASON = "task class not covered by task_modules"
_MAIN_MODULE_REASON = (
    "task class defined in a __main__ module, which no container can import"
)

# Distinct ``(class, reason)`` pairs named in a refusal message before it
# truncates. Well above any plausible number of genuinely-different
# failures, and far below the number of tasks one broken class can produce.
_MAX_LISTED_GROUPS = 20


def plan_rehydration(
    tasks: typing.Iterable[BaseTask],
    patterns: typing.Sequence[str],
) -> RehydrationPlan:
    """Decide, per task, whether a scheduler tick could rebuild it.

    The self-check reconstructs the task from exactly the payload that
    registration stores — the instance body, ``task.instance_body()`` (see
    ``_get_task_data_for_registration``) — so it is a faithful dry run of
    what a scheduler tick will do.

    ``AliasTask`` payloads fail the check by construction (rehydration
    refuses ``__aliased`` data, whose pickled ``loads_type`` would be an
    execution primitive in a scheduler process), as do dynamically
    generated and otherwise non-importable classes, and any field whose
    serialization does not round-trip to the same id.
    """
    reconstructable: list[BaseTask] = []
    unreconstructable: list[tuple[BaseTask, str]] = []
    for task in tasks:
        module = type(task).__module__
        if module_is_main(module):
            # Reported apart from plain non-coverage because the remedy
            # differs, and the plain one would be a lie here: no pattern
            # reaches a __main__ module (see :func:`module_is_main`).
            unreconstructable.append((task, _MAIN_MODULE_REASON))
            continue
        if not module_is_covered(module, patterns):
            unreconstructable.append((task, _UNCOVERED_REASON))
            continue
        try:
            task_from_registry_data(task.instance_body(), expected_task_id=task.id)
        except Exception as e:
            unreconstructable.append((task, f"registry-data round-trip failed ({e})"))
            continue
        reconstructable.append(task)
    return RehydrationPlan(
        reconstructable=tuple(reconstructable),
        unreconstructable=tuple(unreconstructable),
    )
