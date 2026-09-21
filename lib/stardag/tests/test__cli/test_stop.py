"""Tests for `stardag builds stop` and the selection rules behind it.

Two layers, deliberately. :mod:`stardag._cli._stop` holds the rules — which
rows are this build's to stop, what the filters mean — and is tested
directly, because those are the statements the whole command rests on. The
command itself is exercised through typer's ``CliRunner`` against a mocked
registry, for the parts that only exist at that level: the ordering (stop,
then cancel), the dry run, and the confirmation.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from stardag._cli import _stop
from stardag._cli.builds import app
from stardag.registry import TaskListPage, TaskSummary

runner = CliRunner(env={"COLUMNS": "240"})

BUILD_ID = "11111111-1111-1111-1111-111111111111"
OTHER_BUILD_ID = "22222222-2222-2222-2222-222222222222"


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


def _row(**overrides) -> TaskSummary:
    """A task row holding a live Modal execution under ``BUILD_ID``."""
    data = {
        "id": uuid4(),
        "task_id": "task-" + uuid4().hex[:8],
        "task_namespace": "acme.features",
        "task_name": "Featurise",
        "latest_status": "running",
        "latest_status_at": _ago(minutes=20),
        "latest_status_build_id": UUID(BUILD_ID),
        "latest_executor": "modal",
        "latest_executor_ref": "fc-" + uuid4().hex[:8],
        "latest_executor_metadata": {
            "kind": "modal",
            "app_name": "pipeline",
            "workspace": "acme",
            "function_name": "worker_gpu",
        },
    }
    data.update(overrides)
    return TaskSummary.model_validate(data)


def _execution(**overrides) -> _stop.Execution:
    execution = _stop.execution_from_task(_row(**overrides))
    assert execution is not None
    return execution


def _mock_registry(rows, total=None):
    registry = mock.MagicMock()
    registry.task_list.return_value = TaskListPage(
        tasks=list(rows),
        total=len(rows) if total is None else total,
        page=1,
        page_size=100,
    )
    return registry


def _patch_resolve(registry):
    return mock.patch("stardag._cli.builds._resolve_registry", return_value=registry)


class TestSelection:
    """Which rows are this build's to stop."""

    @pytest.mark.parametrize("status", ["running", "interrupted"])
    def test_a_status_that_may_still_have_a_container(self, status):
        assert _stop.execution_from_task(_row(latest_status=status)) is not None

    @pytest.mark.parametrize(
        "status", ["suspended", "pending", "completed", "failed", "cancelled"]
    )
    def test_a_status_that_cannot(self, status):
        # SUSPENDED is the interesting one: it keeps its executor ref, but
        # that execution yielded and returned, so there is nothing to stop.
        assert _stop.execution_from_task(_row(latest_status=status)) is None

    def test_no_ref_means_no_execution_to_stop(self):
        assert _stop.execution_from_task(_row(latest_executor_ref=None)) is None

    def test_a_ref_with_no_executor_named_is_treated_as_modal(self):
        # Pre-``latest_executor`` data. Modal is the only executor that has
        # ever recorded a ref, and dropping the row would hide a live
        # container from the one list that is supposed to be exact.
        execution = _execution(latest_executor=None)
        assert execution.executor == "modal"
        assert execution.stoppable

    def test_only_this_builds_rows_are_collected(self):
        mine = _row()
        theirs = _row(latest_status_build_id=UUID(OTHER_BUILD_ID))
        registry = _mock_registry([mine, theirs])

        collected = _stop.collect_executions(registry, UUID(BUILD_ID))

        assert [e.task_id for e in collected] == [mine.task_id]

    def test_the_scan_pages_until_the_server_says_it_is_done(self):
        registry = mock.MagicMock()
        first = [_row() for _ in range(100)]
        second = [_row() for _ in range(5)]
        registry.task_list.side_effect = [
            TaskListPage(tasks=first, total=105, page=1, page_size=100),
            TaskListPage(tasks=second, total=105, page=2, page_size=100),
        ]

        collected = _stop.collect_executions(registry, UUID(BUILD_ID))

        assert len(collected) == 105
        assert registry.task_list.call_count == 2

    def test_a_pathological_environment_refuses_rather_than_truncating(self):
        registry = mock.MagicMock()
        registry.task_list.return_value = TaskListPage(
            tasks=[_row() for _ in range(100)],
            # More than the scan will ever page through.
            total=10_000_000,
            page=1,
            page_size=100,
        )
        with pytest.raises(_stop.TooManyClaimHolders):
            _stop.collect_executions(registry, UUID(BUILD_ID))


class TestExecutionFields:
    def test_worker_name_has_the_modal_prefix_stripped(self):
        assert _execution().worker == "gpu"

    def test_an_unprefixed_function_name_is_reported_as_is(self):
        execution = _execution(
            latest_executor_metadata={"kind": "modal", "function_name": "gpu"}
        )
        assert execution.worker == "gpu"

    def test_no_metadata_means_no_worker(self):
        assert _execution(latest_executor_metadata=None).worker is None

    def test_qualified_name_omits_an_empty_namespace(self):
        assert _execution(task_namespace="").qualified_name == "Featurise"
        assert _execution().qualified_name == "acme.features.Featurise"

    def test_a_non_modal_executor_is_listed_but_not_stoppable(self):
        assert not _execution(latest_executor="prefect").stoppable

    def test_restart_due_only_while_the_preemption_is_outstanding(self):
        # The restart records its own start, which moves latest_status_at
        # past the preemption — so this goes false with nothing to clear.
        outstanding = _execution(
            latest_status_at=_ago(minutes=20),
            latest_preempted_at=_ago(minutes=2),
        )
        landed = _execution(
            latest_status_at=_ago(minutes=1),
            latest_preempted_at=_ago(minutes=2),
        )
        assert outstanding.restart_due
        assert not landed.restart_due
        assert not _execution().restart_due


class TestFilters:
    def test_no_filter_matches_everything(self):
        assert _stop.Filters().matches(_execution())

    def test_worker(self):
        filters = _stop.Filters(worker="gpu")
        assert filters.matches(_execution())
        assert not filters.matches(
            _execution(latest_executor_metadata={"function_name": "worker_cpu"})
        )

    def test_executor(self):
        assert _stop.Filters(executor="modal").matches(_execution())
        assert not _stop.Filters(executor="modal").matches(
            _execution(latest_executor="prefect")
        )

    def test_namespace_is_a_prefix(self):
        assert _stop.Filters(namespace="acme").matches(_execution())
        assert _stop.Filters(namespace="acme.features").matches(_execution())
        assert not _stop.Filters(namespace="acme.labels").matches(_execution())

    def test_task_id_is_exact_and_repeatable(self):
        one = _execution()
        two = _execution()
        filters = _stop.Filters(task_ids=(one.task_id,))
        assert filters.matches(one)
        assert not filters.matches(two)

    def test_older_than(self):
        filters = _stop.Filters(older_than_seconds=600)
        assert filters.matches(_execution(latest_status_at=_ago(minutes=20)))
        assert not filters.matches(_execution(latest_status_at=_ago(minutes=2)))

    def test_older_than_never_matches_an_undatable_row(self):
        # An age that cannot be established is not evidence of age — the
        # same rule the server applies to ``status_older_than``.
        filters = _stop.Filters(older_than_seconds=600)
        assert not filters.matches(_execution(latest_status_at=None))

    def test_filters_are_conjunctive(self):
        filters = _stop.Filters(worker="gpu", namespace="acme.labels")
        assert not filters.matches(_execution())


class TestStopCommand:
    def test_dry_run_prints_the_list_and_touches_nothing(self):
        registry = _mock_registry([_row(task_name="Featurise")])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])

        assert result.exit_code == 0
        assert "Featurise" in result.output
        assert "Dry run" in result.output
        cancel.assert_not_called()
        registry.build_cancel.assert_not_called()

    def test_stops_the_calls_before_it_cancels_the_build(self):
        # The one ordering the whole command exists for. Asserted as an
        # order rather than as two facts: cancelling the build first
        # releases the claims, and from that moment the list this command
        # acted on is no longer a statement about anything.
        row = _row()
        registry = _mock_registry([row])
        order = []
        registry.build_cancel.side_effect = lambda *a, **k: order.append("cancel")

        def _cancel_calls(executions):
            order.append("stop")
            return [_stop.CancelOutcome(e) for e in executions]

        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls", side_effect=_cancel_calls),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        assert order == ["stop", "cancel"]
        registry.build_cancel.assert_called_once_with(UUID(BUILD_ID), cascade=True)

    def test_only_the_selected_workers_calls_are_cancelled(self):
        gpu = _row(latest_executor_metadata={"function_name": "worker_gpu"})
        cpu = _row(latest_executor_metadata={"function_name": "worker_cpu"})
        registry = _mock_registry([gpu, cpu])

        with (
            _patch_resolve(registry),
            mock.patch.object(
                _stop,
                "cancel_modal_calls",
                side_effect=lambda es: [_stop.CancelOutcome(e) for e in es],
            ) as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--worker", "gpu", "--yes"])

        assert result.exit_code == 0, result.output
        stopped = [e.executor_ref for e in cancel.call_args.args[0]]
        assert stopped == [gpu.latest_executor_ref]
        # ...and the build is still cancelled. The excluded execution keeps
        # running with its claim released, which the output has to say.
        registry.build_cancel.assert_called_once()
        assert "excluded by a filter" in result.output

    def test_a_non_modal_execution_is_listed_and_left_alone(self):
        registry = _mock_registry([_row(latest_executor="prefect")])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])

        assert "not stoppable here" in result.output
        cancel.assert_not_called()

    def test_a_failed_cancel_is_reported_and_the_build_is_still_cancelled(self):
        # Refusing to release the claims because one call could not be
        # reached would strand every other task of the build too, and the
        # operator has the per-call list in front of them.
        registry = _mock_registry([_row(), _row()])

        def _cancel_calls(executions):
            return [
                _stop.CancelOutcome(executions[0], error="NotFoundError: gone"),
                _stop.CancelOutcome(executions[1]),
            ]

        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls", side_effect=_cancel_calls),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        assert "failed" in result.output
        assert "Modal dashboard" in result.output
        registry.build_cancel.assert_called_once()

    def test_modal_missing_leaves_the_build_untouched(self):
        # The claims are what keep the list exact, so a command that cannot
        # stop anything must not release them: re-running it has to be
        # possible and has to see the same thing.
        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(
                _stop,
                "cancel_modal_calls",
                side_effect=_stop.ModalUnavailable("no modal"),
            ),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--yes"])

        assert result.exit_code == 1
        registry.build_cancel.assert_not_called()

    def test_nothing_to_stop_says_where_the_executions_went(self):
        registry = _mock_registry([])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])

        assert result.exit_code == 0
        assert "holds no running executions" in result.output
        assert "frontier" in result.output

    def test_aborts_without_confirmation(self):
        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID], input="n\n")

        assert result.exit_code != 0
        cancel.assert_not_called()
        registry.build_cancel.assert_not_called()

    def test_the_prompt_says_how_many_are_left_running(self):
        registry = _mock_registry(
            [
                _row(latest_executor_metadata={"function_name": "worker_gpu"}),
                _row(latest_executor_metadata={"function_name": "worker_cpu"}),
            ]
        )
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["stop", BUILD_ID, "--worker", "gpu"], input="n\n"
            )

        assert "1 execution(s) will be left running" in result.output

    def test_rejects_a_non_uuid_build_id(self):
        result = runner.invoke(app, ["stop", "not-a-uuid", "--dry-run"])
        assert result.exit_code == 1
        assert "not a valid build ID" in result.output

    def test_older_than_rejects_bad_grammar(self):
        result = runner.invoke(
            app, ["stop", BUILD_ID, "--older-than", "soon", "--dry-run"]
        )
        assert result.exit_code == 1
        assert "Invalid duration" in result.output

    def test_older_than_allows_a_short_window(self):
        # Unlike the build-staleness flags, which enforce a 60s floor: here
        # the operator is looking at their own build's executions, and
        # nothing acts on the answer unattended.
        registry = _mock_registry([_row(latest_status_at=_ago(seconds=90))])
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["stop", BUILD_ID, "--older-than", "30s", "--dry-run"]
            )
        assert result.exit_code == 0, result.output
        assert "Featurise" in result.output

    def test_json_dry_run_is_parseable(self):
        import json

        registry = _mock_registry([_row(), _row(latest_status="interrupted")])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run", "--json"])

        payload = json.loads(result.stdout)
        assert payload["build_id"] == BUILD_ID
        assert payload["dry_run"] is True
        assert len(payload["selected"]) == 2
        assert {e["status"] for e in payload["selected"]} == {
            "running",
            "interrupted",
        }

    def test_json_stdout_stays_one_document_through_a_real_run(self):
        """Per-call progress must not join the payload on stdout."""
        import json

        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(
                _stop,
                "cancel_modal_calls",
                side_effect=lambda es: [_stop.CancelOutcome(e) for e in es],
            ),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--json", "--yes"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is False
        assert "stopped" not in result.stdout

    def test_json_refuses_to_prompt(self):
        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--json"])

        assert result.exit_code == 1
        cancel.assert_not_called()
        registry.build_cancel.assert_not_called()


class TestCancelNoLongerCascades:
    def test_cascade_is_refused_with_the_new_command(self):
        registry = mock.MagicMock()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--cascade", "--yes"])

        assert result.exit_code == 1
        assert f"stardag builds stop {BUILD_ID}" in result.output
        registry.build_cancel.assert_not_called()

    def test_plain_cancel_stops_nothing_and_says_so(self):
        registry = mock.MagicMock()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        registry.build_cancel.assert_called_once_with(UUID(BUILD_ID))
        assert "Nothing was stopped" in result.output
