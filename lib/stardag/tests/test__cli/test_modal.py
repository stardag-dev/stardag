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


class TestGcRetiresBeforeStopping:
    """``gc`` retires the registry record first and stops the app only once
    the registry agreed; a 409 means a build took the deployment since the
    listing, and it is kept — app running, record live."""

    @staticmethod
    def _deployment(handle: str, running: int = 0):
        from uuid import uuid4

        from stardag.registry._base import DeploymentInfo

        return DeploymentInfo(
            id=uuid4(),
            family="fam",
            handle=handle,
            code_id=handle.split("--")[1] * 8,
            running_builds=running,
        )

    def _run(self, monkeypatch, deployments, retire):
        from stardag.registry import NoOpRegistry, registry_provider

        events: list[tuple[str, str]] = []

        class FakeRegistry(NoOpRegistry):
            def deployment_list(self, *, family=None, include_retired=False):
                return list(deployments)

            def deployment_retire(self, deployment_id, *, force=False):
                handle = next(d.handle for d in deployments if d.id == deployment_id)
                events.append(("retire", handle))
                return retire(handle)

        def fake_run(cmd, **kwargs):
            events.append(("stop", cmd[cmd.index("stop") + 1]))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr("subprocess.run", fake_run)
        with registry_provider.override(FakeRegistry()):
            result = runner.invoke(app, ["gc", "fam", "--keep", "1"])
        return result, events

    def test_retire_then_stop_in_that_order(self, monkeypatch):
        newest, older, busy = (
            self._deployment("fam--aaaa"),
            self._deployment("fam--bbbb"),
            self._deployment("fam--cccc", running=2),
        )
        result, events = self._run(
            monkeypatch, [newest, older, busy], retire=lambda handle: None
        )
        assert result.exit_code == 0, result.output
        # Only the older idle one is collected: the newest idle is kept for
        # the resolver, the busy one is never a candidate.
        assert events == [("retire", "fam--bbbb"), ("stop", "fam--bbbb")]
        assert "keep fam--cccc" in result.output

    def test_a_deployment_taken_since_the_listing_is_kept_and_not_stopped(
        self, monkeypatch
    ):
        from stardag.exceptions import APIError

        def retire(handle):
            raise APIError(
                "in use",
                status_code=409,
                payload={"error_code": "deployment_in_use"},
            )

        result, events = self._run(
            monkeypatch,
            [self._deployment("fam--aaaa"), self._deployment("fam--bbbb")],
            retire=retire,
        )
        assert result.exit_code == 0, result.output
        assert events == [("retire", "fam--bbbb")]
        assert "took it since the listing" in result.output
