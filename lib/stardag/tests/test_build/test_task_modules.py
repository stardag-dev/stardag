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
    module_is_main,
    _MAIN_MODULE_REASON,
    _MAX_LISTED_GROUPS,
    RehydrationPlan,
    plan_rehydration,
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


class TestMainModules:
    """``__main__`` is unreachable, and knowably so — which is the point."""

    @pytest.mark.parametrize("module", ["__main__", "my_pkg.__main__", "a.b.__main__"])
    def test_recognised(self, module: str):
        assert module_is_main(module)
        # ...and therefore never covered, by any pattern.
        assert not module_is_covered(module, [suggested_pattern_for(module)])
        assert not module_is_covered(module, [module])

    @pytest.mark.parametrize(
        "module", ["__main__x", "my_pkg.main", "my_pkg.__main__x", "main"]
    )
    def test_not_confused_with_ordinary_modules(self, module: str):
        assert not module_is_main(module)


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
# The rehydration pre-flight
# =============================================================================


class TestPlanRehydration:
    def test_a_covered_round_tripping_task_is_reconstructable(self):
        task = SyncOnlyTask(name="rebuild-me")
        plan = plan_rehydration([task], [SyncOnlyTask.__module__])
        assert plan.reconstructable == (task,)
        assert plan.unreconstructable == ()
        assert plan.error([SyncOnlyTask.__module__]) is None
        assert "1 task(s) reconstructable, 0 not" in plan.summary()

    def test_an_uncovered_class_is_not(self):
        task = SyncOnlyTask(name="unreachable")
        plan = plan_rehydration([task], ["unrelated_pkg.*"])
        assert plan.reconstructable == ()
        assert [reason for _, reason in plan.unreconstructable] == [
            "task class not covered by task_modules"
        ]
        assert "not covered" in plan.summary()

    def test_alias_task_payloads_are_refused(self, default_in_memory_fs_target):
        """AliasTask embeds a pickled ``loads_type``; rehydration refuses it
        (auto-unpickling registry bytes in a scheduler is an RCE vector), so
        the self-check fails — which now means a reactive build containing an
        incomplete AliasTask is refused at the trigger. That loses nothing: an
        AliasTask has no ``run()``, so a tick could never have scheduled it."""
        import stardag as sd

        class PreflightAliasSource(sd.Task[int]):
            def run(self) -> None:
                self._save(42)

        source = PreflightAliasSource()
        alias = sd.AliasTask[int](aliased=sd.AliasedMetadata.from_task(source))
        plan = plan_rehydration([alias], [type(alias).__module__, __name__])

        assert plan.reconstructable == ()
        assert len(plan.unreconstructable) == 1
        assert "round-trip failed" in plan.unreconstructable[0][1]
        assert "__aliased" in plan.unreconstructable[0][1]

    def test_the_error_names_the_class_the_reason_and_the_remedy(self):
        tasks = [SyncOnlyTask(name="x"), SyncOnlyTask(name="y")]
        plan = plan_rehydration(tasks, ["unrelated_pkg.*"])
        error = plan.error(["unrelated_pkg.*"])
        assert error is not None
        assert "2 task(s)" in error
        assert f"{SyncOnlyTask.__module__}.{SyncOnlyTask.__qualname__}" in error
        assert "not covered by task_modules" in error
        # The remedy: the narrowest pattern that would cover the class.
        assert suggested_pattern_for(SyncOnlyTask.__module__) in error
        assert "['unrelated_pkg.*']" in error

    def test_the_listing_is_grouped_with_one_example_task(self):
        """One broken class over a fan-out is the normal shape of a refusal.

        Listing every task would put thousands of identical lines in the
        log and in the ``error_message`` recorded on the build; one example
        id per (class, reason) is what makes it diagnosable.
        """
        tasks = [SyncOnlyTask(name=f"t{i}") for i in range(50)]
        plan = plan_rehydration(tasks, ["unrelated_pkg.*"])
        error = plan.error(["unrelated_pkg.*"])
        assert error is not None
        listed = [line for line in error.splitlines() if line.startswith("  - ")]
        assert len(listed) == 1
        assert "(and 49 more task(s), same reason)" in error
        # The example is a real task of the build.
        assert any(str(task.id) in error for task in tasks)

    def test_one_class_with_task_specific_reasons_is_not_collapsed(self):
        """Grouping by class alone would pick one arbitrary reason and then
        attach the whole class's count to it.

        A round-trip reason embeds the exception, so two tasks of one class
        can genuinely fail differently. Built directly rather than through
        `plan_rehydration`, because that is the surface under test: the
        message must not claim a count spans a reason it does not.
        """
        a, b, c = (SyncOnlyTask(name=n) for n in ("a", "b", "c"))
        plan = RehydrationPlan(
            unreconstructable=(
                (a, "registry-data round-trip failed (field x)"),
                (b, "registry-data round-trip failed (field x)"),
                (c, "registry-data round-trip failed (field y)"),
            )
        )
        error = plan.error(["some_pkg.*"])
        assert error is not None
        listed = [line for line in error.splitlines() if line.startswith("  - ")]
        assert len(listed) == 2
        assert "(field x)" in error and "(field y)" in error
        # The count belongs to the reason it is printed beside.
        assert "(and 1 more task(s), same reason)" in error
        assert "(and 2 more" not in error
        # A round-trip failure has no one-line remedy; don't invent one.
        assert "Add [" not in error

    def test_many_distinct_classes_truncate(self):
        import stardag as sd

        classes = [
            type(
                f"TruncatedTask{i}",
                (sd.Task[int],),
                {"__module__": "acme_unreachable.tasks", "run": lambda self: None},
            )
            for i in range(_MAX_LISTED_GROUPS + 5)
        ]
        plan = plan_rehydration([cls() for cls in classes], ["unrelated_pkg.*"])
        error = plan.error(["unrelated_pkg.*"])
        assert error is not None
        listed = [line for line in error.splitlines() if line.startswith("  - ")]
        assert len(listed) == _MAX_LISTED_GROUPS + 1  # + the "...and N more" line
        assert "...and 5 further class/reason group(s)." in error

    def test_summary_aggregates_repeated_reasons(self):
        tasks = [SyncOnlyTask(name=f"t{i}") for i in range(3)]
        plan = plan_rehydration(tasks, ["unrelated_pkg.*"])
        assert "(x3)" in plan.summary()

    def test_a_main_module_class_gets_its_own_reason(self):
        """Not plain non-coverage: no pattern reaches a ``__main__`` module.

        ``module_is_covered`` excludes one outright, so the generic remedy
        would be a lie — and the ``my_pkg.__main__`` shape is the dangerous
        one, because ``suggested_pattern_for`` produces ``my_pkg.*``, which
        reads as plausible and changes nothing.
        """
        import stardag as sd

        entrypoint_task = type(
            "EntrypointTask",
            (sd.Task[int],),
            {"__module__": "my_pkg.__main__", "run": lambda self: None},
        )()
        plan = plan_rehydration([entrypoint_task], ["my_pkg.*"])

        assert plan.reconstructable == ()
        assert [reason for _, reason in plan.unreconstructable] == [_MAIN_MODULE_REASON]
        error = plan.error(["my_pkg.*"])
        assert error is not None
        assert "cannot be covered by any pattern" in error
        assert "my_pkg.__main__" in error
        # ...and it must NOT tell them to add the pattern they already have.
        assert "Add [" not in error

    def test_main_and_ordinary_uncovered_classes_each_get_their_remedy(self):
        import stardag as sd

        entrypoint_task = type(
            "BothEntrypointTask",
            (sd.Task[int],),
            {"__module__": "__main__", "run": lambda self: None},
        )()
        plan = plan_rehydration(
            [entrypoint_task, SyncOnlyTask(name="ordinary")], ["unrelated_pkg.*"]
        )
        error = plan.error(["unrelated_pkg.*"])
        assert error is not None
        assert suggested_pattern_for(SyncOnlyTask.__module__) in error
        assert "cannot be covered by any pattern" in error
        assert "'__main__'" in error

    def test_no_patterns_means_nothing_is_reconstructable(self):
        task = SyncOnlyTask(name="no-patterns")
        plan = plan_rehydration([task], [])
        assert plan.reconstructable == ()
        assert len(plan.unreconstructable) == 1
        assert "not declared" in (plan.error([]) or "")

    def test_the_dry_run_uses_the_payload_registration_stores(self):
        """The registry-mode dump, not a full one.

        ``task_data`` holds identity parameters only; a task's level 2/3
        fields are resolved from the build config wherever it is rebuilt.
        Round-tripping a *full* dump would check a payload the registry does
        not hold — and would reject a task whose non-identity field does not
        round-trip, even though a tick never sees that field in the data.
        """
        import typing as t

        import stardag as sd
        from stardag.base_model import StardagField

        class NotRoundTrippable:
            """Serializes to a string, validates from anything but one."""

        class PreflightConfigured(sd.Task[int]):
            __namespace__ = "preflight_tests"
            key: str
            width: t.Annotated[int, StardagField(significance="dependencies_only")] = 4

            def run(self) -> None:
                pass

        from stardag.build_config import build_config_scope

        with build_config_scope({"preflight_tests.PreflightConfigured": {"width": 9}}):
            task = PreflightConfigured(key="k")
        assert task.width == 9

        # No config installed here, so a rebuilt task gets width=4. The dry
        # run must still pass: width is not part of the identity, so the id
        # is unchanged and the payload round-trips.
        plan = plan_rehydration([task], [PreflightConfigured.__module__])
        assert plan.reconstructable == (task,)


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
