"""Tests for CLI config commands, specifically profile management."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from stardag.cli.config import app
from stardag.cli.credentials import add_registry, list_profiles

runner = CliRunner()


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory for stardag config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        config_path = tmppath / "config.toml"

        # Patch the config module path functions
        with mock.patch("stardag.config.get_stardag_dir", return_value=tmppath):
            with mock.patch(
                "stardag.config.get_user_config_path", return_value=config_path
            ):
                with mock.patch(
                    "stardag.cli.credentials.get_user_config_path",
                    return_value=config_path,
                ):
                    yield tmppath, config_path


class TestProfileAdd:
    """Test profile add command validation."""

    def test_profile_add_fails_when_registry_not_found(self, temp_config_dir):
        """Test that profile add fails when registry doesn't exist."""
        result = runner.invoke(
            app,
            [
                "profile",
                "add",
                "test-profile",
                "-r",
                "nonexistent-registry",
                "-u",
                "test@example.com",
                "-o",
                "test-org",
                "-w",
                "test-workspace",
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_profile_add_fails_when_org_cannot_be_verified(self, temp_config_dir):
        """Test that profile add fails when organization cannot be verified."""
        _ = temp_config_dir  # Fixture needed for side effects (config path patching)

        # Add a registry first
        add_registry("test-registry", "http://localhost:8000")

        # Mock resolve_org_slug_to_id to return None (can't verify)
        with mock.patch("stardag.cli.config.resolve_org_slug_to_id", return_value=None):
            result = runner.invoke(
                app,
                [
                    "profile",
                    "add",
                    "test-profile",
                    "-r",
                    "test-registry",
                    "-u",
                    "test@example.com",
                    "-o",
                    "unverifiable-org",
                    "-w",
                    "test-workspace",
                ],
            )
            assert result.exit_code != 0
            assert "could not verify organization" in result.output.lower()

    def test_profile_add_fails_when_workspace_cannot_be_verified(self, temp_config_dir):
        """Test that profile add fails when workspace cannot be verified."""
        _ = temp_config_dir  # Fixture needed for side effects (config path patching)

        # Add a registry first
        add_registry("test-registry", "http://localhost:8000")

        # Mock resolve_org_slug_to_id to return org_id
        # Mock resolve_workspace_slug_to_id to return None (can't verify)
        with mock.patch(
            "stardag.cli.config.resolve_org_slug_to_id",
            return_value="org-123",
        ):
            with mock.patch(
                "stardag.cli.config.resolve_workspace_slug_to_id",
                return_value=None,
            ):
                result = runner.invoke(
                    app,
                    [
                        "profile",
                        "add",
                        "test-profile",
                        "-r",
                        "test-registry",
                        "-u",
                        "test@example.com",
                        "-o",
                        "test-org",
                        "-w",
                        "unverifiable-workspace",
                    ],
                )
                assert result.exit_code != 0
                assert "could not verify workspace" in result.output.lower()

    def test_profile_add_succeeds_when_org_and_workspace_verified(
        self, temp_config_dir
    ):
        """Test that profile add succeeds when both org and workspace are verified."""
        _ = temp_config_dir  # Fixture needed for side effects (config path patching)

        # Add a registry first
        add_registry("test-registry", "http://localhost:8000")

        # Mock both resolve functions to return valid IDs
        with mock.patch(
            "stardag.cli.config.resolve_org_slug_to_id",
            return_value="org-123",
        ):
            with mock.patch(
                "stardag.cli.config.resolve_workspace_slug_to_id",
                return_value="ws-456",
            ):
                result = runner.invoke(
                    app,
                    [
                        "profile",
                        "add",
                        "test-profile",
                        "-r",
                        "test-registry",
                        "-u",
                        "test@example.com",
                        "-o",
                        "my-org",
                        "-w",
                        "my-workspace",
                    ],
                )
                assert result.exit_code == 0
                assert "added" in result.output.lower()

                # Verify profile was actually persisted
                profiles = list_profiles()
                assert "test-profile" in profiles
                assert profiles["test-profile"]["registry"] == "test-registry"
                assert profiles["test-profile"]["organization"] == "my-org"
                assert profiles["test-profile"]["workspace"] == "my-workspace"

    def test_profile_add_with_default_flag_refreshes_token(self, temp_config_dir):
        """Test that profile add with --default flag attempts token refresh."""
        _ = temp_config_dir  # Fixture needed for side effects (config path patching)

        add_registry("test-registry", "http://localhost:8000")

        with mock.patch(
            "stardag.cli.config.resolve_org_slug_to_id",
            return_value="org-123",
        ):
            with mock.patch(
                "stardag.cli.config.resolve_workspace_slug_to_id",
                return_value="ws-456",
            ):
                with mock.patch(
                    "stardag.cli.config.ensure_access_token",
                    return_value="mock-token",
                ) as mock_ensure_token:
                    result = runner.invoke(
                        app,
                        [
                            "profile",
                            "add",
                            "default-profile",
                            "-r",
                            "test-registry",
                            "-u",
                            "test@example.com",
                            "-o",
                            "my-org",
                            "-w",
                            "my-workspace",
                            "--default",
                        ],
                    )
                    assert result.exit_code == 0
                    assert "default profile" in result.output.lower()
                    assert "refreshed" in result.output.lower()
                    # Verify token refresh was attempted
                    mock_ensure_token.assert_called_once_with(
                        "test-registry", "org-123", "test@example.com"
                    )

    def test_profile_add_with_uuid_org_and_workspace(self, temp_config_dir):
        """Test that profile add works when org/workspace are already UUIDs."""
        _ = temp_config_dir  # Fixture needed for side effects (config path patching)

        add_registry("test-registry", "http://localhost:8000")

        # When passing UUIDs, resolve functions return the UUID unchanged
        org_uuid = "550e8400-e29b-41d4-a716-446655440000"
        ws_uuid = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

        with mock.patch(
            "stardag.cli.config.resolve_org_slug_to_id",
            return_value=org_uuid,
        ):
            with mock.patch(
                "stardag.cli.config.resolve_workspace_slug_to_id",
                return_value=ws_uuid,
            ):
                result = runner.invoke(
                    app,
                    [
                        "profile",
                        "add",
                        "uuid-profile",
                        "-r",
                        "test-registry",
                        "-u",
                        "test@example.com",
                        "-o",
                        org_uuid,
                        "-w",
                        ws_uuid,
                    ],
                )
                assert result.exit_code == 0
                assert "added" in result.output.lower()
                # Should NOT show "Verified" messages when IDs match input
                assert "verified organization" not in result.output.lower()
                assert "verified workspace" not in result.output.lower()
