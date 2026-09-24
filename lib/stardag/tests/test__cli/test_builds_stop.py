"""``stardag builds stop`` over the execution ledger, against the in-memory
registry: list the unended executions, cancel the Modal calls, report each
one stopped, then cancel the build."""

from __future__ import annotations

import json
from unittest import mock

from typer.testing import CliRunner

from stardag._cli import _stop
from stardag._cli.builds import app
from stardag.build._registration import new_id
from stardag.registry import ExecutionInfo

runner = CliRunner(env={"COLUMNS": "240"})


def _from_id():
    return mock.patch("modal.FunctionCall.from_id")


class TestStop:
    def test_stops_reports_and_cancels(self, fake_registry, running_build):
        with _from_id() as from_id:
            result = runner.invoke(app, ["stop", str(running_build.build_id), "--yes"])
        assert result.exit_code == 0, result.output
        from_id.assert_called_once_with("fc-leaf")
        from_id.return_value.cancel.assert_called_once()
        (reported,) = fake_registry.calls_to("execution_report_stopped")
        assert reported["execution_id"] == running_build.execution_id
        execution = fake_registry.executions[running_build.execution_id]
        assert execution.outcome == "stopped"
        # The server's rule: a revocation is not a result.
        assert execution.claim_outcome == "cancelled"
        assert fake_registry.status_of(running_build.leaf.id) == "cancelled"
        assert fake_registry.builds[running_build.build_id].status == "cancelled"
        assert fake_registry.build_list_executions(running_build.build_id) == []

    def test_dry_run_writes_nothing(self, fake_registry, running_build):
        with _from_id() as from_id:
            result = runner.invoke(
                app, ["stop", str(running_build.build_id), "--dry-run", "--json"]
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        (selected,) = payload["selected"]
        assert selected["executor_ref"] == "fc-leaf"
        assert selected["stoppable"] is True
        assert selected["worker"] == "default"
        from_id.assert_not_called()
        assert not fake_registry.called("execution_report_stopped")
        assert fake_registry.builds[running_build.build_id].status == "running"

    def test_no_cancel_leaves_the_build(self, fake_registry, running_build):
        with _from_id():
            result = runner.invoke(
                app, ["stop", str(running_build.build_id), "--yes", "--no-cancel"]
            )
        assert result.exit_code == 0, result.output
        assert fake_registry.called("execution_report_stopped")
        assert not fake_registry.called("build_cancel")

    def test_orphan_filter_asks_the_server_and_never_cancels(
        self, fake_registry, running_build
    ):
        with _from_id() as from_id:
            result = runner.invoke(
                app,
                ["stop", str(running_build.build_id), "--not-in-current-plan", "-y"],
            )
        assert result.exit_code == 0, result.output
        (listed,) = fake_registry.calls_to("build_list_executions")
        assert listed["not_in_current_plan"] is True
        # The one execution is in the active plan: nothing is an orphan.
        from_id.assert_not_called()
        assert not fake_registry.called("build_cancel")
        assert "no orphaned executions" in result.output

    def test_orphan_filter_json_emits_a_document_even_with_nothing_selected(
        self, fake_registry, running_build
    ):
        with _from_id() as from_id:
            result = runner.invoke(
                app,
                [
                    "stop",
                    str(running_build.build_id),
                    "--not-in-current-plan",
                    "--yes",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["selected"] == []
        assert payload["stop_results"] == []
        assert payload["stopped_count"] == 0
        assert payload["lost"] == []
        assert payload["build_cancelled"] is False
        from_id.assert_not_called()
        assert not fake_registry.called("build_cancel")

    def test_a_failed_cancel_is_not_reported_stopped(
        self, fake_registry, running_build
    ):
        with _from_id() as from_id:
            from_id.return_value.cancel.side_effect = RuntimeError("gone")
            result = runner.invoke(
                app, ["stop", str(running_build.build_id), "--yes", "--json"]
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        (outcome,) = payload["stop_results"]
        assert outcome["stopped"] is False and outcome["reported"] is False
        assert not fake_registry.called("execution_report_stopped")
        assert payload["build_cancelled"] is True

    def test_json_without_yes_refuses_before_any_output(
        self, fake_registry, running_build
    ):
        result = runner.invoke(app, ["stop", str(running_build.build_id), "--json"])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert not fake_registry.called("build_list_executions")

    def test_a_task_filter_excludes_and_says_so(self, fake_registry, running_build):
        with _from_id() as from_id:
            result = runner.invoke(
                app,
                ["stop", str(running_build.build_id), "--task-id", "other", "-y"],
            )
        assert result.exit_code == 0, result.output
        from_id.assert_not_called()
        assert "excluded by a filter" in result.output

    def test_a_namespace_filter_selects_by_prefix(self, fake_registry, running_build):
        """v1's ``--namespace``: a prefix of the task's namespace, read per
        listed task from ``GET /tasks/{id}`` (the ledger rows carry none)."""
        leaf = str(running_build.leaf.id)
        fake_registry.tasks[leaf].task_namespace = "acme.features"
        for prefix, selected in (("acme", 1), ("acme.features", 1), ("acme.lab", 0)):
            result = runner.invoke(
                app,
                [
                    "stop",
                    str(running_build.build_id),
                    "--namespace",
                    prefix,
                    "--dry-run",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.stdout)
            assert len(payload["selected"]) == selected, prefix
            assert len(payload["excluded_by_filter"]) == 1 - selected

    def test_without_namespace_no_task_is_read(self, fake_registry, running_build):
        with mock.patch.object(fake_registry, "task_get") as task_get:
            runner.invoke(app, ["stop", str(running_build.build_id), "--dry-run"])
        task_get.assert_not_called()


class TestFilters:
    def _execution(self, task_id: str = "t1") -> ExecutionInfo:
        return ExecutionInfo.model_validate({"id": str(new_id()), "task_id": task_id})

    def test_namespace_is_a_prefix_and_an_unknown_one_never_matches(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        spaces = {"t1": "acme.features"}
        execution = self._execution()
        assert _stop.Filters(namespace="acme").matches(
            execution, now=now, namespaces=spaces
        )
        assert not _stop.Filters(namespace="acme.labels").matches(
            execution, now=now, namespaces=spaces
        )
        assert not _stop.Filters(namespace="acme").matches(
            self._execution("t2"), now=now, namespaces=spaces
        )
        assert _stop.Filters(namespace="acme").any_set


class TestMarkLost:
    def _without_call_id(self, fake_registry, running_build):
        fake_registry.executions[running_build.execution_id].executor_ref = None

    def test_marks_an_uncancellable_execution_lost(self, fake_registry, running_build):
        self._without_call_id(fake_registry, running_build)
        with _from_id() as from_id:
            result = runner.invoke(
                app,
                ["stop", str(running_build.build_id), "--mark-lost", "--no-cancel"],
                input="y\ny\n",
            )
        assert result.exit_code == 0, result.output
        from_id.assert_not_called()
        (reported,) = fake_registry.calls_to("execution_report_stopped")
        assert reported["outcome"] == "lost"
        execution = fake_registry.executions[running_build.execution_id]
        assert (execution.outcome, execution.claim_outcome) == ("lost", "cancelled")
        assert "will ever be applied" in result.output
        assert "marked 1 lost" in result.output

    def test_declining_the_second_prompt_marks_nothing(
        self, fake_registry, running_build
    ):
        self._without_call_id(fake_registry, running_build)
        result = runner.invoke(
            app,
            ["stop", str(running_build.build_id), "--mark-lost"],
            input="y\nn\n",
        )
        assert result.exit_code == 1
        assert not fake_registry.called("execution_report_stopped")
        assert not fake_registry.called("build_cancel")

    def test_without_the_flag_nothing_is_marked(self, fake_registry, running_build):
        self._without_call_id(fake_registry, running_build)
        result = runner.invoke(app, ["stop", str(running_build.build_id), "-y"])
        assert result.exit_code == 0, result.output
        assert not fake_registry.called("execution_report_stopped")

    def test_the_fake_refuses_an_unknown_outcome(self, fake_registry, running_build):
        import pytest

        from stardag.exceptions import APIError

        with pytest.raises(APIError):
            fake_registry.execution_report_stopped(
                running_build.execution_id,
                outcome="gone",  # type: ignore[arg-type]
            )


class TestStoppable:
    def _execution(self, **fields) -> ExecutionInfo:
        return ExecutionInfo.model_validate({"id": str(new_id()), **fields})

    def test_modal_with_a_call_id_is_stoppable(self):
        assert _stop.is_stoppable(
            self._execution(executor="modal", executor_ref="fc-1")
        )

    def test_modal_claim_before_its_spawn_is_listed_not_stoppable(self):
        execution = self._execution(executor_metadata={"kind": "modal"})
        assert _stop.not_stoppable_reason(execution) == _stop.NO_REF_YET

    def test_no_executor_is_ambiguous(self):
        assert _stop.not_stoppable_reason(self._execution()) == _stop.NO_EXECUTOR

    def test_another_executor_is_never_stoppable(self):
        reason = _stop.not_stoppable_reason(self._execution(executor="local"))
        assert reason is not None and "'local'" in reason
