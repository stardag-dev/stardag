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


class TestDeploymentRecord:
    """Every deploy is one deployment row: created **before** the deploy under
    the id the app baked into its secret, activated **after** it. A failure
    of either exits non-zero, with the remedy."""

    def _app(self):
        from unittest.mock import MagicMock
        from uuid import uuid4

        return MagicMock(code_id="c" * 40, deployment_id=uuid4())

    def test_create_records_the_baked_id_and_activate_makes_it_current(self):
        from stardag._cli._modal_deployments import (
            _activate_deployment,
            _create_deployment,
        )
        from stardag.testing import InMemoryRegistry

        registry = InMemoryRegistry()
        stardag_app = self._app()
        _create_deployment(registry, stardag_app, "myapp")
        (created,) = registry.calls_to("deployment_create")
        assert created["deployment_id"] == stardag_app.deployment_id
        assert (created["kind"], created["app_name"], created["code_id"]) == (
            "modal",
            "myapp",
            "c" * 40,
        )
        assert (
            registry.deployment_list(kind="modal", app_name="myapp", current=True) == []
        )
        _activate_deployment(registry, stardag_app.deployment_id, "myapp")
        (current,) = registry.deployment_list(
            kind="modal", app_name="myapp", current=True
        )
        assert current.id == stardag_app.deployment_id

    @pytest.mark.parametrize("step", ["create", "activate"])
    def test_a_failure_exits_non_zero(self, step):
        import typer

        from stardag._cli._modal_deployments import (
            _activate_deployment,
            _create_deployment,
        )
        from stardag.testing import InMemoryRegistry

        class Failing(InMemoryRegistry):
            def deployment_create(self, **kwargs):
                if step == "create":
                    raise ConnectionError("registry unreachable")
                return super().deployment_create(**kwargs)

            def deployment_activate(self, deployment_id):
                raise ConnectionError("registry unreachable")

        registry = Failing()
        stardag_app = self._app()
        with pytest.raises(typer.Exit) as excinfo:
            _create_deployment(registry, stardag_app, "myapp")
            _activate_deployment(registry, stardag_app.deployment_id, "myapp")
        assert excinfo.value.exit_code == 1

    def test_without_a_registry_nothing_is_recorded(self):
        from stardag._cli._modal_deployments import _deployment_registry
        from stardag.registry import NoOpRegistry, registry_provider

        with registry_provider.override(NoOpRegistry()):
            assert _deployment_registry() is None


class TestDeploymentsListing:
    """``stardag modal deployments`` lists what the registry recorded and
    marks each app's current one."""

    def test_lists_with_the_current_marked(self):
        from stardag.registry import registry_provider
        from stardag.testing import InMemoryRegistry

        registry = InMemoryRegistry()
        old = registry.add_deployment(kind="modal", app_name="myapp", code_id="a" * 40)
        new = registry.add_deployment(kind="modal", app_name="myapp", code_id="b" * 40)
        assert old != new
        with registry_provider.override(registry):
            result = CliRunner(env={"COLUMNS": "240"}).invoke(
                app, ["deployments", "--app", "myapp"]
            )
        assert result.exit_code == 0, result.output
        assert "bbbbbbbbbbbb" in result.output
        assert "aaaaaaaaaaaa" in result.output
        assert "current" in result.output
        (listed,) = registry.calls_to("deployment_list")
        assert listed["app_name"] == "myapp"

    def test_without_a_registry_it_returns_with_the_notice(self):
        """Through ``_deployment_registry()``, like ``modal deploy``: the
        no-op registry has no ``deployment_list`` to call."""
        from stardag.registry import NoOpRegistry, registry_provider

        with registry_provider.override(NoOpRegistry()):
            result = CliRunner(env={"COLUMNS": "240"}).invoke(app, ["deployments"])
        assert result.exit_code == 0, result.output
        assert "No registry configured; no deployments to list." in result.output
        assert "Deployments" not in result.output
