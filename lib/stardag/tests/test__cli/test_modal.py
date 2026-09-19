"""Tests for the `stardag modal` CLI.

Regression test for https://github.com/stardag-dev/stardag/issues/148
where `from modal.environments import ensure_env` broke on modal >= 1.4.3
because `ensure_env` moved to the private `modal._environments` module.
"""

import pytest
from typer.testing import CliRunner

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag._cli.modal import app

runner = CliRunner()


def test_deploy_reaches_module_import():
    """`deploy` should get past its modal imports and env resolution.

    With a nonexistent script path, the command must fail with the
    "Error importing module" message — not an ImportError from the
    modal imports that precede it (issue #148).
    """
    result = runner.invoke(app, ["deploy", "nonexistent_script_xyz.py"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"deploy raised unexpectedly: {result.exception!r}"
    )
    assert result.exit_code == 1
    assert "Error importing module" in result.output


class TestRecordDeployment:
    """Every deploy is recorded: one row per (app, code id), Modal's own
    notion of a deployment. The Modal app id rides along when the deploy
    reported one; nothing else is derived from it."""

    def _run(self, modal_app_id):
        from unittest.mock import MagicMock

        from stardag._cli.modal import _record_deployment
        from stardag.registry import NoOpRegistry, registry_provider

        recorded: list[tuple[str, str, str | None]] = []

        class FakeRegistry(NoOpRegistry):
            def deployment_record(self, *, app_name, code_id, modal_app_id=None):
                recorded.append((app_name, code_id, modal_app_id))
                return None

        stardag_app = MagicMock(code_id="c" * 40)
        with registry_provider.override(FakeRegistry()):
            _record_deployment(stardag_app, "myapp", modal_app_id=modal_app_id)
        return recorded

    def test_a_deploy_is_recorded_under_the_app_name(self):
        assert self._run("ap-123") == [("myapp", "c" * 40, "ap-123")]

    def test_without_a_modal_app_id_the_record_still_lands(self):
        assert self._run(None) == [("myapp", "c" * 40, None)]


class TestDeploymentsListing:
    """``stardag modal deployments`` lists what the registry recorded, newest
    first, and marks the current one per app."""

    def test_lists_newest_first_with_the_current_marked(self):
        from datetime import datetime, timezone
        from uuid import uuid4

        from stardag.registry import NoOpRegistry, registry_provider
        from stardag.registry._base import DeploymentInfo

        rows = [
            DeploymentInfo(
                id=uuid4(),
                app_name="myapp",
                code_id="b" * 40,
                deployed_at=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
                current=True,
                modal_app_id="ap-new",
            ),
            DeploymentInfo(
                id=uuid4(),
                app_name="myapp",
                code_id="a" * 40,
                deployed_at=datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc),
            ),
        ]
        asked: list[str | None] = []

        class FakeRegistry(NoOpRegistry):
            def deployment_list(self, *, app_name=None):
                asked.append(app_name)
                return rows

        with registry_provider.override(FakeRegistry()):
            result = runner.invoke(app, ["deployments", "--app", "myapp"])
        assert result.exit_code == 0, result.output
        assert asked == ["myapp"]
        assert result.output.index("bbbbbbbbbbbb") < result.output.index("aaaaaaaaaaaa")
        assert "current" in result.output
        assert "ap-new" in result.output
        assert "gc" not in [c.name for c in app.registered_commands]
