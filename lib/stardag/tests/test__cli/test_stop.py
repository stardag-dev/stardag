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

    def test_a_claim_with_no_ref_yet_is_listed_and_not_stoppable(self):
        # STA-88, and the reason this is not "no ref, no row": the tick
        # starts a task twice. First a claim, which sets RUNNING with no
        # ref because nothing has been spawned yet; then a ref-bearing
        # start once the spawn returns a call id. Dropping the row in
        # between made the list silently short — two of four running
        # upstreams on the live run that found this — at exactly the
        # moment an operator reaches for the command, during a fan-out.
        execution = _execution(latest_executor_ref=None)
        assert execution.executor_ref is None
        assert not execution.stoppable
        assert execution.not_stoppable_reason == _stop.NO_REF_YET

    def test_a_ref_with_no_executor_named_is_treated_as_modal(self):
        # Pre-``latest_executor`` data. Modal is the only executor that has
        # ever recorded a ref, and dropping the row would hide a live
        # container from the one list that is supposed to be exact.
        execution = _execution(latest_executor=None, latest_executor_metadata=None)
        assert execution.executor == "modal"
        assert execution.stoppable

    def test_a_non_detached_execution_is_not_attributed_to_modal(self):
        # The row a local, thread-pool or subprocess execution writes:
        # ``TaskExecutorABC.get_executor_metadata`` defaults to None and
        # the non-detached start passes no executor, no ref and no
        # metadata (``build/_concurrent.py``'s ``registry_task_start``
        # with ``handle is None``, and ``_sequential.py``'s plain
        # ``task_start``). Guessing Modal here would put it under
        # ``--executor modal`` and promise a call id that never arrives.
        execution = _execution(
            latest_executor=None,
            latest_executor_ref=None,
            latest_executor_metadata=None,
        )
        assert execution.executor == ""
        assert not execution.stoppable
        assert execution.not_stoppable_reason == _stop.NO_EXECUTOR

    def test_an_unspawned_claim_is_attributed_from_its_metadata(self):
        # A claim names no executor of its own — there is no call yet — but
        # its metadata declares the kind it is about to spawn on. Reading
        # it keeps ``--executor`` honest about a row that would otherwise
        # be assumed to be Modal.
        execution = _execution(
            latest_executor=None,
            latest_executor_ref=None,
            latest_executor_metadata={"kind": "prefect"},
        )
        assert execution.executor == "prefect"
        assert not execution.stoppable

    def test_a_mid_spawn_fan_out_is_listed_in_full(self):
        # The shape of the live failure: a fan-out caught partway, some
        # tasks spawned and some only claimed. Every one of them is this
        # build's and every one of them is running; the list has to say so
        # for all four, or the operator releases claims believing they
        # stopped more than they did.
        spawned = [_row(), _row()]
        claimed = [_row(latest_executor_ref=None), _row(latest_executor_ref=None)]
        registry = _mock_registry(spawned + claimed)

        collected, _ = _stop.collect_executions(registry, UUID(BUILD_ID))

        assert len(collected) == 4
        assert sum(e.stoppable for e in collected) == 2

    def test_only_this_builds_rows_are_collected(self):
        mine = _row()
        theirs = _row(latest_status_build_id=UUID(OTHER_BUILD_ID))
        registry = _mock_registry([mine, theirs])

        collected, _ = _stop.collect_executions(registry, UUID(BUILD_ID))

        assert [e.task_id for e in collected] == [mine.task_id]

    def test_the_scan_pages_until_the_server_says_it_is_done(self):
        registry = mock.MagicMock()
        first = [_row() for _ in range(100)]
        second = [_row() for _ in range(5)]
        registry.task_list.side_effect = [
            TaskListPage(tasks=first, total=105, page=1, page_size=100),
            TaskListPage(tasks=second, total=105, page=2, page_size=100),
        ]

        collected, _ = _stop.collect_executions(registry, UUID(BUILD_ID))

        assert len(collected) == 105
        assert registry.task_list.call_count == 2

    def test_a_server_that_ignores_the_status_filter_is_detected(self):
        # It answers with every task rather than rejecting the unknown
        # param, so the answer stays right (every row is re-checked) while
        # the scan walks the whole table. The command says so.
        registry = _mock_registry([_row(), _row(latest_status="completed")])
        collected, server_filtered = _stop.collect_executions(registry, UUID(BUILD_ID))
        assert len(collected) == 1
        assert server_filtered is False

        with _patch_resolve(_mock_registry([_row(), _row(latest_status="pending")])):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "does not support filtering tasks by status" in result.output

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

    def test_an_unspawned_claim_is_selected_reported_and_not_cancelled(self):
        # The end-to-end form of STA-88. The row is selected — it is this
        # build's and it matches the filter, both of which the claim's own
        # metadata already establishes — and it reaches neither the
        # canceller nor silence: the operator is told it keeps running,
        # because that is the one thing this command must never get wrong.
        spawned = _row()
        claimed = _row(latest_executor_ref=None)
        registry = _mock_registry([spawned, claimed])

        with (
            _patch_resolve(registry),
            mock.patch.object(
                _stop,
                "cancel_modal_calls",
                side_effect=lambda es: [_stop.CancelOutcome(e) for e in es],
            ) as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        handed_to_modal = [e.task_id for e in cancel.call_args.args[0]]
        assert handed_to_modal == [spawned.task_id]
        assert "could not be stopped" in result.output
        assert claimed.task_id in result.output
        # Not the filter's wording: nobody asked for this one to be left.
        assert "excluded by a filter" not in result.output
        registry.build_cancel.assert_called_once()

    def test_the_json_document_says_why_a_selection_was_not_stopped(self):
        import json

        registry = _mock_registry([_row(latest_executor_ref=None)])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--json", "--yes"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        (entry,) = payload["selected"]
        assert entry["executor_ref"] is None
        assert entry["stoppable"] is False
        assert entry["not_stoppable_reason"] == _stop.NO_REF_YET
        assert payload["stopped_count"] == 0
        cancel.assert_not_called()

    def test_an_unattributed_row_is_not_called_permanently_unstoppable(self):
        # No executor, no ref, no metadata. Written by a non-detached
        # execution *and* by a Modal claim whose best-effort
        # ``get_executor_metadata`` returned None, so neither verdict is
        # safe. The output names both and points at the one action that
        # tells them apart, rather than picking.
        registry = _mock_registry(
            [
                _row(
                    latest_executor=None,
                    latest_executor_ref=None,
                    latest_executor_metadata=None,
                )
            ]
        )
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])

        assert result.exit_code == 0, result.output
        # Whitespace-normalised: rich wraps the notice to the terminal
        # width, so a phrase can straddle a line break.
        rendered = " ".join(result.output.split())
        assert "no call id on their row" in rendered
        assert "own process" in rendered
        cancel.assert_not_called()

    def test_a_ref_less_non_modal_row_is_not_offered_a_re_run(self):
        # Both ways of being unstoppable at once. "Re-run to catch it" is
        # true of a claim waiting on its spawn and false of an execution
        # stardag can never reach, so the notice keys on the reason rather
        # than on the ref being absent.
        registry = _mock_registry(
            [_row(latest_executor="prefect", latest_executor_ref=None)]
        )
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--dry-run"])

        assert "re-run this command" not in result.output
        assert "not stoppable here" in result.output
        cancel.assert_not_called()

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

    def test_the_prompt_leads_with_how_many_keep_running(self):
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

        assert "1 execution(s) will keep running" in result.output

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

    def test_json_document_reports_what_happened(self):
        """Written after the run, so every field is about the past."""
        import json

        row = _row()
        registry = _mock_registry([row])
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
        assert payload["build_cancelled"] is True
        assert payload["stopped_count"] == 1
        assert payload["stop_results"] == [
            {
                "task_id": row.task_id,
                "executor_ref": row.latest_executor_ref,
                "stopped": True,
                "error": None,
            }
        ]

    def test_an_aborted_run_writes_no_document_at_all(self):
        """The failure this replaces: a complete-looking document from a
        run that stopped nothing and cancelled nothing. A caller reading
        stdout cannot see an exit code, so the document must not exist
        unless it is true."""
        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(
                _stop,
                "cancel_modal_calls",
                side_effect=_stop.ModalUnavailable("no modal"),
            ),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--json", "--yes"])

        assert result.exit_code == 1
        assert result.stdout == ""
        registry.build_cancel.assert_not_called()

    def test_a_call_that_could_not_be_stopped_is_in_the_document(self):
        # The build is still cancelled -- refusing to release the claims
        # over one unreachable call would strand every other task -- so
        # the per-call entry is the only place a partial stop is visible.
        import json

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
            result = runner.invoke(app, ["stop", BUILD_ID, "--json", "--yes"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["build_cancelled"] is True
        assert payload["stopped_count"] == 1
        assert [r["stopped"] for r in payload["stop_results"]] == [False, True]
        assert payload["stop_results"][0]["error"] == "NotFoundError: gone"

    def test_json_refuses_to_prompt_before_writing_anything(self):
        # The refusal has to come *before* the document: a caller parsing
        # stdout would otherwise get a complete, successful-looking
        # selection from a run that exited non-zero and stopped nothing.
        registry = _mock_registry([_row()])
        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls") as cancel,
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--json"])

        assert result.exit_code == 1
        assert result.stdout == ""
        cancel.assert_not_called()
        registry.build_cancel.assert_not_called()

    def test_a_cancel_failure_with_brackets_is_not_eaten_as_markup(self):
        # An exception message is arbitrary text; rich would swallow a
        # `[...]` in it, so the one line saying which call could not be
        # stopped would come out mangled.
        registry = _mock_registry([_row()])

        def _cancel_calls(executions):
            return [
                _stop.CancelOutcome(
                    executions[0], error="NotFoundError: [fc-abc] unknown id"
                )
            ]

        with (
            _patch_resolve(registry),
            mock.patch.object(_stop, "cancel_modal_calls", side_effect=_cancel_calls),
        ):
            result = runner.invoke(app, ["stop", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        assert "[fc-abc] unknown id" in result.output


class TestCancelNoLongerCascades:
    def test_cascade_is_refused_with_the_new_command(self):
        registry = mock.MagicMock()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--cascade", "--yes"])

        assert result.exit_code == 1
        assert f"stardag builds stop {BUILD_ID}" in result.output
        registry.build_cancel.assert_not_called()

    def test_no_cascade_still_parses(self):
        # It asked for exactly today's behaviour, so failing a script that
        # spells it out would be gratuitous.
        registry = mock.MagicMock()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--no-cascade", "--yes"])

        assert result.exit_code == 0, result.output
        registry.build_cancel.assert_called_once_with(UUID(BUILD_ID))

    def test_plain_cancel_stops_nothing_and_says_so(self):
        registry = mock.MagicMock()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--yes"])

        assert result.exit_code == 0, result.output
        registry.build_cancel.assert_called_once_with(UUID(BUILD_ID))
        assert "Nothing was stopped" in result.output
