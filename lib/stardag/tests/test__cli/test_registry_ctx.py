"""Tests for the shared registry-client acquisition used by CLI groups.

``_resolve_registry`` is what every registry-backed command calls before
it does anything, so its profile/environment resolution is exercised here
once rather than once per command group.
"""

from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

from stardag._cli._registry_ctx import _resolve_registry
from stardag._cli.limits import app

runner = CliRunner()

_ENV_UUID = "12345678-1234-1234-1234-1234567890ab"


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


class TestNoRegistry:
    def test_no_registry_configured(self):
        """Without a configured registry the command exits with a clear error."""
        fake_config = mock.MagicMock()
        fake_config.registry = None
        with (
            mock.patch(
                "stardag._cli._registry_ctx.get_config", return_value=fake_config
            ),
            mock.patch("stardag._cli._registry_ctx.clear_config_cache"),
        ):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "No registry configured" in result.output


class TestResolveEnv:
    """``-e/--stardag-env`` accepts either a raw environment UUID or a slug."""

    def test_raw_uuid_used_directly(self):
        """A UUID is used as-is, without registry/workspace context or slug lookup."""
        config = _fake_config()
        with (
            mock.patch("stardag._cli._registry_ctx.get_config", return_value=config),
            mock.patch("stardag._cli._registry_ctx.clear_config_cache"),
            mock.patch("stardag._cli._registry_ctx.APIRegistry") as api_registry,
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
            mock.patch("stardag._cli._registry_ctx.get_config", return_value=config),
            mock.patch("stardag._cli._registry_ctx.clear_config_cache"),
            mock.patch("stardag._cli._registry_ctx.APIRegistry") as api_registry,
        ):
            _resolve_registry(None, _ENV_UUID)
        api_registry.assert_called_once_with(environment_id=_ENV_UUID)

    def test_slug_resolved_via_lookup(self):
        """A non-UUID value is resolved slug->id using registry/workspace context."""
        config = _fake_config()
        with (
            mock.patch("stardag._cli._registry_ctx.get_config", return_value=config),
            mock.patch("stardag._cli._registry_ctx.clear_config_cache"),
            mock.patch("stardag._cli._registry_ctx.APIRegistry") as api_registry,
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
            mock.patch("stardag._cli._registry_ctx.get_config", return_value=config),
            mock.patch("stardag._cli._registry_ctx.clear_config_cache"),
            mock.patch("stardag._cli._registry_ctx.APIRegistry"),
        ):
            with pytest.raises(typer.Exit):
                _resolve_registry(None, "my-env-slug")
