"""Tests for the `stardag concurrency-limits` CLI.

Commands are exercised with typer's ``CliRunner`` against a mocked
registry client, so no network / real registry is required.
"""

from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

from stardag._cli.limits import app, _resolve_registry
from stardag.exceptions import APIError

runner = CliRunner()


def _fake_config(
    *,
    registry_name: str | None = "reg",
    workspace_id: str | None = "ws-1",
    env_id: str | None = None,
):
    """Build a config mock with a configured registry, as _resolve_registry reads it."""
    config = mock.MagicMock()
    config.registry.url = "http://test.invalid"
    config.registry.environment_id = env_id
    config.registry.workspace_id = workspace_id
    config.registry.auth.user_email = "user@example.com"
    config.context.registry_name = registry_name
    return config


def _mock_registry(**methods):
    """Build a MagicMock APIRegistry with the given method return values."""
    registry = mock.MagicMock()
    for name, value in methods.items():
        getattr(registry, name).return_value = value
    return registry


def _patch_resolve(registry):
    """Patch ``_resolve_registry`` to return the given mock registry."""
    return mock.patch("stardag._cli.limits._resolve_registry", return_value=registry)


class TestList:
    def test_list_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_list=[
                {"key": "shards", "max_concurrent": 3},
                {"key": "gpu", "max_concurrent": 1},
            ]
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "shards" in result.output
        assert "gpu" in result.output
        registry.concurrency_limit_list.assert_called_once_with()
        registry.close.assert_called_once()

    def test_list_empty(self):
        registry = _mock_registry(concurrency_limit_list=[])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "No concurrency limits" in result.output

    def test_list_with_holders(self):
        registry = _mock_registry(
            concurrency_limit_list=[{"key": "shards", "max_concurrent": 3}],
            concurrency_limit_holders={"key": "shards", "holders": [], "total": 2},
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--holders"])
        assert result.exit_code == 0, result.output
        assert "Holders" in result.output
        # holder count fetched per key
        registry.concurrency_limit_holders.assert_called_once_with("shards", limit=1)
        assert "2" in result.output


class TestSet:
    def test_set_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_set={"key": "shards", "max_concurrent": 5}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["set", "shards", "5"])
        assert result.exit_code == 0, result.output
        assert "max_concurrent=5" in result.output
        registry.concurrency_limit_set.assert_called_once_with("shards", 5)

    def test_set_rejects_below_one(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["set", "shards", "0"])
        assert result.exit_code == 1
        assert "must be >= 1" in result.output
        registry.concurrency_limit_set.assert_not_called()


class TestDelete:
    def test_delete_with_yes(self):
        registry = _mock_registry(concurrency_limit_delete=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["delete", "shards", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Deleted concurrency limit 'shards'" in result.output
        registry.concurrency_limit_delete.assert_called_once_with("shards")

    def test_delete_aborts_without_confirm(self):
        registry = _mock_registry(concurrency_limit_delete=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["delete", "shards"], input="n\n")
        assert result.exit_code != 0
        registry.concurrency_limit_delete.assert_not_called()


class TestHolders:
    def test_holders_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_holders={
                "key": "shards",
                "holders": [
                    {
                        "task_id": "task-abc",
                        "task_namespace": "walkthrough",
                        "task_name": "ProcessShard",
                        "latest_status_at": "2026-07-14T10:00:00Z",
                        "latest_executor": "modal",
                    }
                ],
                "total": 1,
            }
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["holders", "shards"])
        assert result.exit_code == 0, result.output
        assert "task-abc" in result.output
        assert "walkthrough.ProcessShard" in result.output
        assert "modal" in result.output
        registry.concurrency_limit_holders.assert_called_once_with("shards", limit=100)

    def test_holders_empty(self):
        registry = _mock_registry(
            concurrency_limit_holders={"key": "shards", "holders": [], "total": 0}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["holders", "shards"])
        assert result.exit_code == 0, result.output
        assert "No current holders" in result.output


class TestEvict:
    def test_evict_with_yes(self):
        registry = _mock_registry(
            concurrency_limit_evict={"task_id": "task-abc", "status": "failed"}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["evict", "shards", "task-abc", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Evicted" in result.output
        assert "task-abc" in result.output
        registry.concurrency_limit_evict.assert_called_once_with("shards", "task-abc")

    def test_evict_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["evict", "shards", "task-abc"], input="n\n")
        assert result.exit_code != 0
        registry.concurrency_limit_evict.assert_not_called()


class TestErrorHandling:
    def test_api_error_reported(self):
        registry = mock.MagicMock()
        registry.concurrency_limit_list.side_effect = APIError(
            "List concurrency limits failed", status_code=403, detail="denied"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        # client still closed on failure
        registry.close.assert_called_once()

    def test_no_registry_configured(self):
        """Without a configured registry the command exits with a clear error."""
        fake_config = mock.MagicMock()
        fake_config.registry = None
        with mock.patch("stardag._cli.limits.get_config", return_value=fake_config):
            with mock.patch("stardag._cli.limits.clear_config_cache"):
                result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "No registry configured" in result.output


_ENV_UUID = "12345678-1234-1234-1234-1234567890ab"


class TestResolveEnv:
    """``-e/--stardag-env`` accepts either a raw environment UUID or a slug."""

    def test_raw_uuid_used_directly(self):
        """A UUID is used as-is, without registry/workspace context or slug lookup."""
        config = _fake_config()
        with (
            mock.patch("stardag._cli.limits.get_config", return_value=config),
            mock.patch("stardag._cli.limits.clear_config_cache"),
            mock.patch("stardag._cli.limits.APIRegistry") as api_registry,
            mock.patch(
                "stardag._cli.credentials.resolve_environment_slug_to_id"
            ) as resolve,
        ):
            _resolve_registry(None, _ENV_UUID)
        # No slug resolution attempted for a UUID value.
        resolve.assert_not_called()
        api_registry.assert_called_once_with(environment_id=_ENV_UUID)

    def test_raw_uuid_works_without_registry_workspace_context(self):
        """A UUID resolves even when registry_name / workspace_id are absent."""
        config = _fake_config(registry_name=None, workspace_id=None)
        with (
            mock.patch("stardag._cli.limits.get_config", return_value=config),
            mock.patch("stardag._cli.limits.clear_config_cache"),
            mock.patch("stardag._cli.limits.APIRegistry") as api_registry,
        ):
            _resolve_registry(None, _ENV_UUID)
        api_registry.assert_called_once_with(environment_id=_ENV_UUID)

    def test_slug_resolved_via_lookup(self):
        """A non-UUID value is resolved slug->id using registry/workspace context."""
        config = _fake_config()
        with (
            mock.patch("stardag._cli.limits.get_config", return_value=config),
            mock.patch("stardag._cli.limits.clear_config_cache"),
            mock.patch("stardag._cli.limits.APIRegistry") as api_registry,
            mock.patch(
                "stardag._cli.credentials.resolve_environment_slug_to_id",
                return_value=_ENV_UUID,
            ) as resolve,
        ):
            _resolve_registry(None, "my-env-slug")
        resolve.assert_called_once_with(
            "reg", "ws-1", "my-env-slug", "user@example.com"
        )
        api_registry.assert_called_once_with(environment_id=_ENV_UUID)

    def test_slug_requires_registry_and_workspace(self):
        """A slug (non-UUID) still errors without registry/workspace context."""
        config = _fake_config(registry_name=None, workspace_id=None)
        with (
            mock.patch("stardag._cli.limits.get_config", return_value=config),
            mock.patch("stardag._cli.limits.clear_config_cache"),
            mock.patch("stardag._cli.limits.APIRegistry"),
        ):
            with pytest.raises(typer.Exit):
                _resolve_registry(None, "my-env-slug")
