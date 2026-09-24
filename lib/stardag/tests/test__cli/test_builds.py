"""Tests for the ``stardag builds`` CLI group on the v2 registry: ``show``,
``frontier``, ``ticks`` and ``cancel`` (``stop`` and the lifecycle
commands against the in-memory registry are in ``test_builds_stop.py`` and
``test_v2_commands.py``). The registry client is mocked at
``stardag._cli.builds._resolve_registry`` (and ``builds_frontier``'s)."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock
from uuid import uuid4

from typer.testing import CliRunner

from stardag._cli.builds import app
from stardag.exceptions import APIError, NotFoundError
from stardag.registry import BuildFrontier, BuildInfo, SettingsInfo, TickSummaryRecord

# A wide console keeps each rendered row on one line.
runner = CliRunner(env={"COLUMNS": "240"})

BUILD_ID = "11111111-1111-1111-1111-111111111111"
SETTINGS_HASH = "33333333-3333-5333-8333-333333333333"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _mock_registry(**methods):
    registry = mock.MagicMock()
    for name, value in methods.items():
        method = getattr(registry, name)
        if isinstance(value, Exception):
            method.side_effect = value
        else:
            method.return_value = value
    return registry


@contextmanager
def _patch_resolve(registry):
    with (
        mock.patch("stardag._cli.builds._resolve_registry", return_value=registry),
        mock.patch(
            "stardag._cli.builds_frontier._resolve_registry", return_value=registry
        ),
    ):
        yield


def _build(**overrides) -> BuildInfo:
    data = {
        "id": BUILD_ID,
        "name": "spring-otter-42",
        "status": "running",
        "created_at": NOW,
        "root_task_ids": ["root-task-1"],
        "reactive_app_name": "my-app",
    }
    data.update(overrides)
    return BuildInfo.model_validate(data)


def _member(task_id: str, status: str, **overrides):
    data = {
        "task_id": task_id,
        "instance_id": str(uuid4()),
        "instance_hash": f"h-{task_id}",
        "status": status,
        "is_root": False,
        "body": {"__namespace": "demo.pipeline", "__name": "TrainModel"},
    }
    data.update(overrides)
    return data


def _frontier(**overrides) -> BuildFrontier:
    data = {
        "build_id": BUILD_ID,
        "plan_id": str(uuid4()),
        "deployment_id": str(uuid4()),
        "settings_hash": SETTINGS_HASH,
        "sealed": True,
        "plan_complete": False,
        "build_status": "running",
        "runnable": [_member("runnable-1", "pending", is_root=True)],
        "discovery_jobs": [_member("discover-1", "pending")],
        "running": [_member("running-1", "running")],
    }
    data.update(overrides)
    return BuildFrontier.model_validate(data)


def _show_registry(**overrides):
    methods = {
        "build_get": _build(),
        "build_get_frontier": _frontier(),
        "settings_get": SettingsInfo(hash=SETTINGS_HASH, body={"THREADS": "4"}),
        "build_list_executions": [],
    }
    methods.update(overrides)
    return _mock_registry(**methods)


class TestShow:
    def test_happy_path(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "spring-otter-42" in result.output
        assert "my-app" in result.output
        assert "root-task-1" in result.output
        registry.close.assert_called_once()

    def test_rejects_non_uuid(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", "not-a-uuid"])
        assert result.exit_code == 1
        assert "not a valid build ID" in result.output
        registry.build_get.assert_not_called()

    def test_json(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["id"] == BUILD_ID
        assert payload["active_plan"]["settings"] == {"THREADS": "4"}

    def test_a_missing_build_is_reported_and_the_client_closed(self):
        registry = _mock_registry(build_get=NotFoundError("no build"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()

    def test_a_missing_settings_row_reads_as_null(self):
        registry = _show_registry(settings_get=NotFoundError("no settings"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["active_plan"]["settings"] is None

    def test_a_settings_read_failure_other_than_404_propagates(self):
        registry = _show_registry(settings_get=APIError("auth failed", status_code=401))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()


class TestFrontier:
    def test_renders_the_plan_and_its_three_lists(self):
        registry = _mock_registry(build_get_frontier=_frontier())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        for text in (
            SETTINGS_HASH,
            "runnable-1",
            "discover-1",
            "running-1",
            "TrainModel",
        ):
            assert text in result.output

    def test_closure_conflicts_are_surfaced(self):
        frontier = _frontier(
            closure={
                "admitted": 0,
                "conflicts": [{"task_id": "conflicted-1", "fields": ["w"]}],
                "build_failed": True,
            }
        )
        registry = _mock_registry(build_get_frontier=frontier)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert "conflicted-1" in result.output

    def test_json(self):
        registry = _mock_registry(build_get_frontier=_frontier())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID, "--json"])
        payload = json.loads(result.stdout)
        assert payload["runnable"][0]["task_id"] == "runnable-1"

    def test_an_api_error_is_reported(self):
        registry = _mock_registry(
            build_get_frontier=APIError("boom", status_code=500, detail="boom")
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()


class TestTicks:
    def test_renders_summary_fields(self):
        record = TickSummaryRecord(
            outcome="progressed",
            summary={"spawned": 3, "claim_denied": 0, "some_new_counter": 7},
            created_at=NOW,
        )
        registry = _mock_registry(build_list_tick_summaries=[record])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "progressed" in result.output
        assert "spawned=3" in result.output
        assert "some_new_counter=7" in result.output
        assert "claim_denied" not in result.output

    def test_empty_prints_hint(self):
        registry = _mock_registry(build_list_tick_summaries=[])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert "No tick summaries" in result.output


class TestCancel:
    def test_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID], input="n\n")
        assert result.exit_code != 0
        registry.build_cancel.assert_not_called()

    def test_yes_cancels_and_says_nothing_was_stopped(self):
        registry = _mock_registry(build_cancel=_build(status="cancelled"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--yes"])
        assert result.exit_code == 0, result.output
        registry.build_cancel.assert_called_once()
        assert "Nothing was stopped" in result.output
