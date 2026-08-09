"""Tests for the `stardag tasks` CLI.

Commands are exercised with typer's ``CliRunner`` against a mocked
registry client, so no network / real registry is required.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from typer.testing import CliRunner

from stardag._cli.tasks import app
from stardag.exceptions import APIError
from stardag.registry import TaskListPage, TaskSummary

# See test_builds.py: a wide console keeps assertions off the wrap point.
runner = CliRunner(env={"COLUMNS": "240"})

BUILD_ID = "11111111-1111-1111-1111-111111111111"
OWNER_BUILD_ID = "22222222-2222-2222-2222-222222222222"
TASK_ROW_ID = "33333333-3333-3333-3333-333333333333"


def _mock_registry(**methods):
    registry = mock.MagicMock()
    for name, value in methods.items():
        getattr(registry, name).return_value = value
    return registry


def _patch_resolve(registry):
    return mock.patch("stardag._cli.tasks._resolve_registry", return_value=registry)


def _task(**overrides) -> TaskSummary:
    data = {
        "id": TASK_ROW_ID,
        "task_id": "task-abc",
        "task_namespace": "demo.pipeline",
        "task_name": "TrainModel",
        "latest_status": "running",
        "latest_status_at": datetime.now(timezone.utc) - timedelta(hours=5),
        "latest_status_build_id": OWNER_BUILD_ID,
        "latest_executor": "modal",
    }
    data.update(overrides)
    return TaskSummary.model_validate(data)


class TestList:
    def test_shows_claim_holder_and_age(self):
        registry = _mock_registry(
            task_list=TaskListPage(tasks=[_task()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--status", "running"])
        assert result.exit_code == 0, result.output
        assert "demo.pipeline.TrainModel" in result.output
        assert "task-abc" in result.output
        # Who holds the claim, and for how long.
        assert OWNER_BUILD_ID in result.output
        assert "5h" in result.output
        registry.task_list.assert_called_once()
        kwargs = registry.task_list.call_args.kwargs
        assert kwargs["status"] == ["running"]
        assert kwargs["status_older_than"] is None
        registry.close.assert_called_once()

    def test_status_is_repeatable(self):
        registry = _mock_registry(task_list=TaskListPage())
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["list", "--status", "running", "--status", "suspended"]
            )
        assert result.exit_code == 0, result.output
        assert registry.task_list.call_args.kwargs["status"] == [
            "running",
            "suspended",
        ]

    def test_older_than_becomes_an_absolute_cutoff(self):
        registry = _mock_registry(task_list=TaskListPage())
        before = datetime.now(timezone.utc)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "1h"])
        assert result.exit_code == 0, result.output
        cutoff = registry.task_list.call_args.kwargs["status_older_than"]
        assert isinstance(cutoff, datetime)
        # Roughly one hour back, computed client-side.
        delta = before - cutoff
        assert timedelta(minutes=59) < delta < timedelta(minutes=61)

    def test_older_than_rejects_bad_grammar(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "yesterday"])
        assert result.exit_code == 1
        assert "Invalid duration" in result.output
        registry.task_list.assert_not_called()

    def test_empty_prints_hint(self):
        registry = _mock_registry(task_list=TaskListPage())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "No tasks match" in result.output
        assert "stardag tasks list --status running" in result.output

    def test_json_is_parseable_and_uncontaminated(self):
        registry = _mock_registry(
            task_list=TaskListPage(tasks=[_task()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--status", "running", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["tasks"][0]["task_id"] == "task-abc"
        assert payload["tasks"][0]["latest_status_build_id"] == OWNER_BUILD_ID
        assert "─" not in result.stdout

    def test_truncation_hint(self):
        registry = _mock_registry(
            task_list=TaskListPage(tasks=[_task()], total=42, page=1, page_size=1)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "Showing 1 of 42" in result.output


class TestCancel:
    def test_with_yes(self):
        registry = _mock_registry(task_cancel_by_id=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "task-abc", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Cancelled task" in result.output
        args = registry.task_cancel_by_id.call_args.args
        assert str(args[0]) == BUILD_ID
        assert args[1] == "task-abc"

    def test_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "task-abc"], input="n\n")
        assert result.exit_code != 0
        registry.task_cancel_by_id.assert_not_called()

    def test_rejects_non_uuid_build(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", "nope", "task-abc", "--yes"])
        assert result.exit_code == 1
        assert "not a valid build ID" in result.output
        registry.task_cancel_by_id.assert_not_called()


class TestRetry:
    def test_with_yes(self):
        registry = _mock_registry(task_retry_by_id=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["retry", BUILD_ID, "task-abc", "-y"])
        assert result.exit_code == 0, result.output
        assert "Reset task" in result.output
        args = registry.task_retry_by_id.call_args.args
        assert str(args[0]) == BUILD_ID
        assert args[1] == "task-abc"

    def test_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["retry", BUILD_ID, "task-abc"], input="n\n")
        assert result.exit_code != 0
        registry.task_retry_by_id.assert_not_called()


class TestErrorHandling:
    def test_api_error_reported_and_client_closed(self):
        registry = mock.MagicMock()
        registry.task_list.side_effect = APIError(
            "List tasks failed", status_code=403, detail="denied"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        registry.close.assert_called_once()

    def test_cancel_error_reported(self):
        registry = mock.MagicMock()
        registry.task_cancel_by_id.side_effect = APIError(
            "Cancel task failed", status_code=404, detail="Task not found"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "task-abc", "-y"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        registry.close.assert_called_once()
