"""Tests for the ``stardag plans`` CLI group on the v2 registry: ``show``.
The registry client is mocked at ``stardag._cli.plans._resolve_registry``."""

import json
from unittest import mock
from uuid import uuid4

from typer.testing import CliRunner

from stardag._cli.plans import app
from stardag.exceptions import APIError, NotFoundError
from stardag.registry import BuildFrontier, PlanRoots, SettingsInfo

runner = CliRunner(env={"COLUMNS": "240"})

PLAN_ID = "22222222-2222-2222-2222-222222222222"
BUILD_ID = "11111111-1111-1111-1111-111111111111"


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
        "settings_hash": "abc123",
        "roots": [],
    }
    data.update(overrides)
    return PlanRoots.model_validate(data)


def _frontier(**overrides) -> BuildFrontier:
    data = {
        "build_id": BUILD_ID,
        "plan_id": PLAN_ID,
        "deployment_id": str(uuid4()),
        "settings_hash": "abc123",
        "sealed": True,
        "plan_complete": False,
        "build_status": "running",
    }
    data.update(overrides)
    return BuildFrontier.model_validate(data)


def _show_registry(**overrides):
    methods = {
        "plan_roots_info": _plan_roots(),
        "build_get_frontier": _frontier(),
        "settings_get": SettingsInfo(hash="abc123", body={"THREADS": "4"}),
    }
    methods.update(overrides)
    return _mock_registry(**methods)


class TestShow:
    def test_happy_path(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, [PLAN_ID])
        assert result.exit_code == 0, result.output
        registry.close.assert_called_once()

    def test_json_reports_settings(self):
        registry = _show_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, [PLAN_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["settings"] == {"THREADS": "4"}

    def test_a_missing_settings_row_reads_as_null(self):
        registry = _show_registry(settings_get=NotFoundError("no settings"))
        with _patch_resolve(registry):
            result = runner.invoke(app, [PLAN_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["settings"] is None

    def test_a_settings_read_failure_other_than_404_propagates(self):
        registry = _show_registry(settings_get=APIError("auth failed", status_code=401))
        with _patch_resolve(registry):
            result = runner.invoke(app, [PLAN_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()

    def test_a_missing_plan_is_reported_and_the_client_closed(self):
        registry = _mock_registry(plan_roots_info=NotFoundError("no plan"))
        with _patch_resolve(registry):
            result = runner.invoke(app, [PLAN_ID])
        assert result.exit_code == 1
        registry.close.assert_called_once()
