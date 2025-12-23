"""Tests for CLI registry management commands."""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from stardag.cli.credentials import (
    create_registry,
    delete_registry,
    list_registries,
)


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory for stardag config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        registries_dir = tmppath / "registries"
        registries_dir.mkdir()

        # Patch the config module path functions
        with mock.patch("stardag.config.get_stardag_dir", return_value=tmppath):
            with mock.patch(
                "stardag.config.get_registries_dir", return_value=registries_dir
            ):
                with mock.patch(
                    "stardag.config.get_registry_dir",
                    side_effect=lambda r: registries_dir / r,
                ):
                    with mock.patch(
                        "stardag.config.get_registry_config_path",
                        side_effect=lambda r: registries_dir / r / "config.json",
                    ):
                        with mock.patch(
                            "stardag.config.get_registry_credentials_path",
                            side_effect=lambda r: registries_dir
                            / r
                            / "credentials.json",
                        ):
                            with mock.patch(
                                "stardag.config.get_active_registry_path",
                                return_value=tmppath / "active_registry",
                            ):
                                with mock.patch(
                                    "stardag.config.load_active_registry",
                                    return_value="default",
                                ):
                                    yield tmppath, registries_dir


class TestListRegistries:
    def test_returns_empty_when_no_registries(self, temp_config_dir):
        """Test that list_registries returns empty list when no registries exist."""
        _, registries_dir = temp_config_dir
        result = list_registries()
        assert result == []

    def test_returns_registries_when_exist(self, temp_config_dir):
        """Test that list_registries returns all registry directories."""
        _, registries_dir = temp_config_dir
        (registries_dir / "local").mkdir()
        (registries_dir / "central").mkdir()

        result = list_registries()
        assert set(result) == {"local", "central"}


class TestCreateRegistry:
    def test_creates_registry_directory(self, temp_config_dir):
        """Test that create_registry creates the registry directory."""
        _, registries_dir = temp_config_dir
        create_registry("test-registry", "http://localhost:8000")

        registry_dir = registries_dir / "test-registry"
        assert registry_dir.exists()

    def test_creates_config_with_api_url(self, temp_config_dir):
        """Test that create_registry saves the API URL."""
        _, registries_dir = temp_config_dir
        create_registry("test-registry", "http://api.example.com")

        config_path = registries_dir / "test-registry" / "config.json"
        assert config_path.exists()

        with open(config_path) as f:
            config = json.load(f)
        assert config["api_url"] == "http://api.example.com"


class TestDeleteRegistry:
    def test_deletes_existing_registry(self, temp_config_dir):
        """Test that delete_registry removes the registry directory."""
        _, registries_dir = temp_config_dir
        registry_dir = registries_dir / "to-delete"
        registry_dir.mkdir(parents=True)
        (registry_dir / "config.json").write_text("{}")

        result = delete_registry("to-delete")

        assert result is True
        assert not registry_dir.exists()

    def test_returns_false_for_nonexistent_registry(self, temp_config_dir):
        """Test that delete_registry returns False for nonexistent registry."""
        result = delete_registry("nonexistent")
        assert result is False
