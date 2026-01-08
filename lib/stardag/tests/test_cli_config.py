"""Tests for CLI config commands, specifically profile management."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from stardag.cli.config import app
from stardag.cli.credentials import add_registry

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
        _, config_path = temp_config_dir

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
        _, config_path = temp_config_dir

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
        _, config_path = temp_config_dir

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
