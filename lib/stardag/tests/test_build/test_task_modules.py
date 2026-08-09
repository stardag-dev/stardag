"""Unit tests for task-module declaration (stardag.build._task_modules).

Expansion is exercised against a *real* package tree written to disk and
put on ``sys.path``, because the property that matters most — expansion
lists module names without importing them — cannot be observed with mocks.
Each generated module appends its name to a marker file at import time, so
the tests can assert exactly which modules were imported and when.
"""

from __future__ import annotations

import itertools
import sys
import typing
from pathlib import Path

import pytest

from stardag import BaseTask, auto_namespace
from stardag.build._task_modules import (
    TaskModulesError,
    _reset_import_state_for_tests,
    count_registered_task_classes,
    declared_task_module_patterns,
    expand_task_module_patterns,
    format_uncovered_message,
    import_failure_note,
    import_task_modules,
    last_import_failures,
    module_is_covered,
    plan_pickle_elision,
    set_declared_task_module_patterns,
    suggested_pattern_for,
    uncovered_task_classes,
    validate_task_module_patterns,
)
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)


# =============================================================================
# A real package tree on disk
# =============================================================================

_PACKAGE_COUNTER = itertools.count()

# Prepended to every generated module: records the import in a marker file
# whose path is baked in, so imports are observable from the test process
# without importing anything itself.
_IMPORT_MARKER_SOURCE = """\
import pathlib

pathlib.Path({marker!r}).open("a").write(__name__ + "\\n")
"""


class GeneratedPackage(typing.NamedTuple):
    """A package tree written to disk and importable from ``sys.path``."""

    name: str
    root: Path
    marker: Path

    def imported(self) -> list[str]:
        """Modules of this package imported so far, in import order."""
        if not self.marker.exists():
            return []
        return [line for line in self.marker.read_text().splitlines() if line]

    def reset_imports(self) -> None:
        self.marker.unlink(missing_ok=True)


@pytest.fixture
def make_package(tmp_path: Path):
    """Factory writing a uniquely-named package tree and cleaning up after.

    Layout (``<pkg>`` is unique per call so ``sys.modules`` can't leak
    between tests)::

        <pkg>/__init__.py
        <pkg>/_tasks.py          underscore-prefixed: must NOT be skipped
        <pkg>/__main__.py        entrypoint: MUST be skipped
        <pkg>/ingest/__init__.py
        <pkg>/ingest/raw.py
        <pkg>/reporting/__init__.py
        <pkg>/reporting/nested/__init__.py
        <pkg>/reporting/nested/deep.py
    """
    created: list[str] = []

    def _make(*, broken_module: bool = False) -> GeneratedPackage:
        name = f"generated_pkg_{next(_PACKAGE_COUNTER)}"
        root = tmp_path / name
        marker = tmp_path / f"{name}.imports"
        header = _IMPORT_MARKER_SOURCE.format(marker=str(marker))

        def write(relpath: str, extra: str = "") -> None:
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header + extra)

        write("__init__.py")
        write("_tasks.py")
        write("__main__.py")
        write("ingest/__init__.py")
        write("ingest/raw.py")
        write("reporting/__init__.py")
        write("reporting/nested/__init__.py")
        write("reporting/nested/deep.py")
        if broken_module:
            write("broken.py", "raise RuntimeError('missing optional dep')\n")

        created.append(name)
        return GeneratedPackage(name=name, root=root, marker=marker)

    sys.path.insert(0, str(tmp_path))
    try:
        yield _make
    finally:
        sys.path.remove(str(tmp_path))
        for name in created:
            for module in [
                m for m in sys.modules if m == name or m.startswith(name + ".")
            ]:
                del sys.modules[module]
        _reset_import_state_for_tests()


# =============================================================================
# Pattern validation
# =============================================================================


class TestPatternValidation:
    def test_accepts_exact_and_wildcard_patterns(self):
        assert validate_task_module_patterns(
            ["my_pkg.tasks.ingest", "my_pkg.pipelines.*", "my_pkg"]
        ) == ("my_pkg", "my_pkg.pipelines.*", "my_pkg.tasks.ingest")

    def test_dedupes_and_sorts(self):
        assert validate_task_module_patterns(["b.*", "a.*", "b.*"]) == ("a.*", "b.*")

    def test_empty_is_allowed_as_opt_out(self):
        assert validate_task_module_patterns([]) == ()

    @pytest.mark.parametrize(
        "pattern,expected_problem",
        [
            ("", "empty"),
            ("  my_pkg.*", "whitespace"),
            ("my_pkg.* ", "whitespace"),
            ("my_pkg.*.tasks", "only allowed as the final component"),
            ("*.tasks", "only allowed as the final component"),
            ("*", "at least the root package"),
            ("my_pkg.", "not a valid Python identifier"),
            ("my_pkg..tasks", "not a valid Python identifier"),
            ("1st_pkg.tasks", "not a valid Python identifier"),
            ("my-pkg.tasks", "not a valid Python identifier"),
            ("my_pkg.ta*sks", "not a valid Python identifier"),
        ],
    )
    def test_rejects_malformed(self, pattern: str, expected_problem: str):
        with pytest.raises(TaskModulesError) as exc:
            validate_task_module_patterns([pattern])
        assert expected_problem in str(exc.value)
        # Always actionable: the message shows the accepted grammar.
        assert "trailing recursive wildcard" in str(exc.value)

    def test_rejects_non_string(self):
        with pytest.raises(TaskModulesError, match="must be strings"):
            validate_task_module_patterns([typing.cast(str, 42)])


# =============================================================================
# Expansion
# =============================================================================


class TestExpansion:
    def test_wildcard_lists_the_whole_tree_without_importing_submodules(
        self, make_package
    ):
        pkg: GeneratedPackage = make_package()

        expanded = expand_task_module_patterns([f"{pkg.name}.*"])

        assert expanded == [
            pkg.name,
            f"{pkg.name}._tasks",  # underscore modules are NOT skipped
            f"{pkg.name}.ingest",
            f"{pkg.name}.ingest.raw",
            f"{pkg.name}.reporting",
            f"{pkg.name}.reporting.nested",
            f"{pkg.name}.reporting.nested.deep",
        ]
        # __main__ submodules are skipped (entrypoints, run for side effects).
        assert f"{pkg.name}.__main__" not in expanded
        # The heart of the matter: only the ROOT package was imported — its
        # __path__ is the unavoidable entry point to the tree. Everything
        # below it came from the filesystem.
        assert pkg.imported() == [pkg.name]
        assert not [m for m in sys.modules if m.startswith(pkg.name + ".")]

    def test_exact_pattern_imports_nothing_at_all(self, make_package):
        pkg: GeneratedPackage = make_package()

        expanded = expand_task_module_patterns([f"{pkg.name}.ingest.raw"])

        assert expanded == [f"{pkg.name}.ingest.raw"]
        assert pkg.imported() == []

    def test_narrower_wildcard_scopes_the_walk(self, make_package):
        pkg: GeneratedPackage = make_package()

        expanded = expand_task_module_patterns([f"{pkg.name}.reporting.*"])

        assert expanded == [
            f"{pkg.name}.reporting",
            f"{pkg.name}.reporting.nested",
            f"{pkg.name}.reporting.nested.deep",
        ]

    def test_overlapping_patterns_dedupe(self, make_package):
        pkg: GeneratedPackage = make_package()

        expanded = expand_task_module_patterns(
            [f"{pkg.name}.*", f"{pkg.name}.ingest.*", f"{pkg.name}.ingest.raw"]
        )

        assert len(expanded) == len(set(expanded))
        assert expanded == sorted(expanded)

    def test_unimportable_root_raises_rather_than_expanding_to_nothing(self):
        with pytest.raises(TaskModulesError) as exc:
            expand_task_module_patterns(["no_such_package_anywhere.*"])
        assert "could not be imported" in str(exc.value)
        assert "typos" in str(exc.value)

    def test_malformed_pattern_raises_from_expansion_too(self):
        with pytest.raises(TaskModulesError):
            expand_task_module_patterns(["my_pkg.*.tasks"])


# =============================================================================
# Importing
# =============================================================================


class TestImportTaskModules:
    def test_imports_every_module_and_counts_classes(self, make_package):
        pkg: GeneratedPackage = make_package()
        modules = expand_task_module_patterns([f"{pkg.name}.*"])
        pkg.reset_imports()
        del sys.modules[pkg.name]

        report = import_task_modules(modules)

        assert report.failures == {}
        assert sorted(report.imported) == sorted(modules)
        assert sorted(pkg.imported()) == sorted(modules)
        # The generated modules define no task classes.
        assert report.task_classes_registered == 0

    def test_failures_are_warned_about_and_retained(self, make_package):
        pkg: GeneratedPackage = make_package(broken_module=True)
        modules = expand_task_module_patterns([f"{pkg.name}.*"])
        broken = f"{pkg.name}.broken"
        assert broken in modules

        report = import_task_modules(modules)

        # One bad module does not abort the rest.
        assert broken not in report.imported
        assert f"{pkg.name}.ingest.raw" in report.imported
        assert "RuntimeError: missing optional dep" in report.failures[broken]
        # Retained process-wide for the rehydration-failure diagnostic.
        assert last_import_failures() == report.failures
        note = import_failure_note()
        assert broken in note and "likely cause" in note

    def test_no_failures_means_no_diagnostic_note(self, make_package):
        pkg: GeneratedPackage = make_package()
        import_task_modules(expand_task_module_patterns([f"{pkg.name}.*"]))
        assert import_failure_note() == ""

    def test_repeated_calls_are_cached_per_module_list(self, make_package):
        pkg: GeneratedPackage = make_package()
        modules = expand_task_module_patterns([f"{pkg.name}.*"])
        first = import_task_modules(modules)
        pkg.reset_imports()

        second = import_task_modules(list(modules))

        assert second is first
        # Nothing re-executed: a container serving many ticks pays once.
        assert pkg.imported() == []

    def test_counts_only_classes_from_the_named_modules(self):
        class _CountedTask(SyncOnlyTask):
            pass

        assert count_registered_task_classes([__name__]) >= 1
        assert count_registered_task_classes(["definitely.not.a.module"]) == 0
        assert _CountedTask  # referenced so the definition isn't flagged unused


# =============================================================================
# Coverage
# =============================================================================


class TestCoverage:
    @pytest.mark.parametrize(
        "module_name,covered",
        [
            ("my_pkg.tasks", True),
            ("my_pkg.tasks.ingest", True),
            ("my_pkg.tasks.ingest.deep", True),
            ("my_pkg", False),
            ("my_pkg.tasksomething", False),  # prefix, not a package boundary
            ("my_pkg.experiments", False),
            ("other.tasks", False),
            ("my_pkg.exact", True),
            ("my_pkg.exactly", False),
        ],
    )
    def test_wildcard_and_exact_matching(self, module_name: str, covered: bool):
        patterns = ["my_pkg.tasks.*", "my_pkg.exact"]
        assert module_is_covered(module_name, patterns) is covered

    def test_main_modules_are_never_covered(self):
        # Consistent with expansion, which skips them: a class defined in a
        # __main__ module can never be registered in a scheduler container.
        assert module_is_covered("my_pkg.__main__", ["my_pkg.*"]) is False
        assert module_is_covered("__main__", ["__main__"]) is False

    def test_no_patterns_covers_nothing(self):
        assert module_is_covered("my_pkg.tasks", []) is False

    def test_suggested_pattern_is_the_defining_package(self):
        assert suggested_pattern_for("my_pkg.experiments.scratch") == (
            "my_pkg.experiments.*"
        )
        assert suggested_pattern_for("toplevel") == "toplevel"

    def test_uncovered_task_classes_dedupes_and_sorts(self):
        tasks = [
            SyncOnlyTask(name="a"),
            SyncOnlyTask(name="b"),  # same class, reported once
        ]
        uncovered = uncovered_task_classes(tasks, ["nothing_matching.*"])
        assert uncovered == [SyncOnlyTask]

    def test_covered_classes_are_not_reported(self):
        tasks = [SyncOnlyTask(name="a")]
        patterns = [f"{SyncOnlyTask.__module__}"]
        assert uncovered_task_classes(tasks, patterns) == []

    def test_only_unwarned_reports_each_class_once_per_process(self):
        tasks = [SyncOnlyTask(name="once")]
        first = uncovered_task_classes(tasks, ["nope.*"], only_unwarned=True)
        second = uncovered_task_classes(tasks, ["nope.*"], only_unwarned=True)
        assert first == [SyncOnlyTask]
        assert second == []

    def test_message_names_class_patterns_and_the_fix(self):
        message = format_uncovered_message(
            [SyncOnlyTask], ["my_pkg.tasks.*"], remedy="Then redeploy."
        )
        assert f"{SyncOnlyTask.__module__}.{SyncOnlyTask.__qualname__}" in message
        assert "['my_pkg.tasks.*']" in message
        assert suggested_pattern_for(SyncOnlyTask.__module__) in message
        assert "redeploy" in message
        assert "Then redeploy." in message

    def test_message_without_declared_patterns(self):
        message = format_uncovered_message([SyncOnlyTask], [])
        assert "not declared" in message


# =============================================================================
# The ambient declaration
# =============================================================================


class TestDeclaredPatterns:
    def test_set_and_read_back(self):
        previous = declared_task_module_patterns()
        try:
            set_declared_task_module_patterns(["my_pkg.*"])
            assert declared_task_module_patterns() == ("my_pkg.*",)
        finally:
            set_declared_task_module_patterns(previous)

    def test_defaults_to_empty(self):
        previous = declared_task_module_patterns()
        try:
            set_declared_task_module_patterns([])
            assert declared_task_module_patterns() == ()
        finally:
            set_declared_task_module_patterns(previous)


# =============================================================================
# Pickle elision planning
# =============================================================================


class TestPlanPickleElision:
    def test_covered_and_round_tripping_task_needs_no_pickle(self):
        task = SyncOnlyTask(name="elide-me")
        plan = plan_pickle_elision([task], [SyncOnlyTask.__module__])
        assert plan.pickle_free == (task,)
        assert plan.pickled == ()
        assert plan.require_pickle_free_error() is None
        assert "1 task(s) pickle-free, 0 pickled" in plan.summary()

    def test_uncovered_class_keeps_its_pickle(self):
        task = SyncOnlyTask(name="keep-me")
        plan = plan_pickle_elision([task], ["unrelated_pkg.*"])
        assert plan.pickle_free == ()
        assert [reason for _, reason in plan.pickled] == [
            "task class not covered by task_modules"
        ]
        assert "not covered" in plan.summary()

    def test_alias_task_payload_stays_pickle_bound(self, default_in_memory_fs_target):
        """AliasTask embeds a pickled ``loads_type``; rehydration refuses it
        (auto-unpickling registry bytes in a scheduler is an RCE vector), so
        the self-check fails and the pickle is written — by design."""
        import stardag as sd

        class ElisionAliasSource(sd.Task[int]):
            def run(self) -> None:
                self._save(42)

        source = ElisionAliasSource()
        alias = sd.AliasTask[int](aliased=sd.AliasedMetadata.from_task(source))
        plan = plan_pickle_elision([alias], [type(alias).__module__, __name__])

        assert plan.pickle_free == ()
        assert len(plan.pickled) == 1
        assert "round-trip failed" in plan.pickled[0][1]
        assert "__aliased" in plan.pickled[0][1]

    def test_require_pickle_free_error_names_every_task_and_reason(self):
        tasks = [SyncOnlyTask(name="x"), SyncOnlyTask(name="y")]
        plan = plan_pickle_elision(tasks, ["unrelated_pkg.*"])
        error = plan.require_pickle_free_error()
        assert error is not None
        assert "2 task(s)" in error
        for task in tasks:
            assert str(task.id) in error
        assert "not covered by task_modules" in error

    def test_summary_aggregates_repeated_reasons(self):
        tasks = [SyncOnlyTask(name=f"t{i}") for i in range(3)]
        plan = plan_pickle_elision(tasks, ["unrelated_pkg.*"])
        assert "(x3)" in plan.summary()

    def test_no_patterns_means_everything_pickled(self):
        task = SyncOnlyTask(name="no-patterns")
        plan = plan_pickle_elision([task], [])
        assert plan.pickle_free == ()
        assert len(plan.pickled) == 1


def test_base_task_registry_exposes_registered_classes():
    """``count_registered_task_classes`` relies on this accessor."""
    classes = BaseTask._registry().classes()
    assert SyncOnlyTask in classes


class TestDeclarationHygiene:
    def test_a_whitespace_padded_pattern_is_normalised(self):
        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            declared_task_module_patterns,
            set_declared_task_module_patterns,
        )

        _reset_import_state_for_tests()
        set_declared_task_module_patterns([" pkg.tasks ", "pkg.more"])
        assert declared_task_module_patterns() == ("pkg.tasks", "pkg.more")
        _reset_import_state_for_tests()

    def test_an_empty_pattern_is_refused_rather_than_stored(self):
        """It would match no module while still reading as a declaration,
        quietly returning ticks to the pickle path."""
        import pytest

        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            set_declared_task_module_patterns,
        )

        _reset_import_state_for_tests()
        with pytest.raises(ValueError, match="non-empty"):
            set_declared_task_module_patterns(["pkg.tasks", "   "])
        _reset_import_state_for_tests()

    def test_the_test_reset_hook_clears_every_piece_of_ambient_state(self):
        """Otherwise a declaration or a one-shot warning leaks into the next
        test and results depend on execution order."""
        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            _warned_classes,
            declared_task_module_patterns,
            set_declared_task_module_patterns,
        )

        set_declared_task_module_patterns(["pkg.tasks"])
        _warned_classes.add("some.Class")

        _reset_import_state_for_tests()

        assert declared_task_module_patterns() == ()
        assert _warned_classes == set()
