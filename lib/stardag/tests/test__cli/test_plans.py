"""Tests for the ``stardag plans`` CLI group on the v2 registry: ``show``
and ``list``.
The registry client is mocked at ``stardag._cli.plans._resolve_registry``."""

import json
from unittest import mock
from uuid import uuid4

from typer.testing import CliRunner

from stardag._cli.plans import app
from stardag.exceptions import APIError, NotFoundError
from stardag.registry import BuildFrontier, PlanDetail, PlanRoots, SettingsInfo

runner = CliRunner(env={"COLUMNS": "240"})

PLAN_ID = "22222222-2222-2222-2222-222222222222"
BUILD_ID = "11111111-1111-1111-1111-111111111111"
SETTINGS_HASH = "33333333-3333-5333-8333-333333333333"


def _mock_registry(**methods):
    registry = mock.MagicMock()
    for name, value in methods.items():
        method = getattr(registry, name)
        if isinstance(value, Exception):
            method.side_effect = value
        else:
            method.return_value = value
    return registry


def _patch_resolve(registry):
    return mock.patch("stardag._cli.plans._resolve_registry", return_value=registry)


def _plan_roots(**overrides) -> PlanRoots:
    data = {
        "plan_id": PLAN_ID,
        "build_id": BUILD_ID,
        "deployment_id": str(uuid4()),
        "settings_hash": SETTINGS_HASH,
        "roots": [],
    }
    data.update(overrides)
    return PlanRoots.model_validate(data)


def _frontier(**overrides) -> BuildFrontier:
    data = {
        "build_id": BUILD_ID,
        "plan_id": PLAN_ID,
        "deployment_id": str(uuid4()),
        "settings_hash": SETTINGS_HASH,
        "sealed": True,
        "plan_complete": False,
        "build_status": "running",
    }
    data.update(overrides)
    return BuildFrontier.model_validate(data)


def _plan(**overrides) -> PlanDetail:
    data = {
        "id": PLAN_ID,
        "build_id": BUILD_ID,
        "deployment_id": str(uuid4()),
        "settings_hash": SETTINGS_HASH,
        "generation": 1,
        "activated_at": "2026-09-24T00:00:00Z",
        "sealed_at": "2026-09-24T00:00:01Z",
        "is_active": True,
        "member_count": 3,
        "root_count": 1,
        "excluded_count": 1,
        "member_counts": {"completed": 1, "running": 1},
    }
    data.update(overrides)
    return PlanDetail.model_validate(data)


def _show_registry(**overrides):
    methods = {
        "plan_get": _plan(),
        "plan_roots_info": _plan_roots(),
        "build_get_frontier": _frontier(),
        "settings_get": SettingsInfo(hash=SETTINGS_HASH, body={"THREADS": "4"}),
    }
    methods.update(overrides)
    return _mock_registry(**methods)


class TestShow:
    def test_happy_path(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID])
        assert result.exit_code == 0, result.output
        registry.close.assert_called_once()

    def test_json_reports_settings(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["settings"] == {"THREADS": "4"}

    def test_a_missing_settings_row_reads_as_null(self):
        registry = _show_registry(settings_get=NotFoundError("no settings"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["settings"] is None

    def test_a_settings_read_failure_other_than_404_propagates(self):
        registry = _show_registry(settings_get=APIError("auth failed", status_code=401))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()

    def test_a_missing_plan_is_reported_and_the_client_closed(self):
        registry = _mock_registry(plan_get=NotFoundError("no plan"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()

    def test_a_superseded_plan_is_shown_without_a_frontier_read(self):
        registry = _show_registry(
            plan_get=_plan(is_active=False, superseded_at="2026-09-24T02:00:00Z")
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["superseded_at"] is not None
        assert payload["outstanding"] is None
        assert payload["member_counts"] == {"completed": 1, "running": 1}
        registry.build_get_frontier.assert_not_called()

    def test_renders_lifecycle_and_counts(self):
        registry = _show_registry(
            plan_get=_plan(is_active=False, superseded_at="2026-09-24T02:00:00Z")
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", PLAN_ID])
        assert result.exit_code == 0, result.output
        assert "superseded 2026-09-24 02:00:00Z" in result.output
        assert "completed=1, running=1 (+1 excluded)" in result.output


class TestList:
    def test_lists_a_builds_plans(self):
        registry = _mock_registry(
            build_list_plans=[
                _plan(generation=2),
                _plan(
                    id=str(uuid4()),
                    generation=1,
                    is_active=False,
                    superseded_at="2026-09-24T02:00:00Z",
                ),
            ]
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--build", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "active" in result.output and "superseded" in result.output
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--build", BUILD_ID, "--json"])
        assert [p["generation"] for p in json.loads(result.stdout)["plans"]] == [2, 1]
