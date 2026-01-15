"""Tests for CLI registry management commands."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from stardag._cli.credentials import (
    add_registry,
    list_registries,
    remove_registry,
)


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
                    "stardag._cli.credentials.get_user_config_path",
                    return_value=config_path,
                ):
                    yield tmppath, config_path


class TestListRegistries:
    def test_returns_empty_when_no_registries(self, temp_config_dir):
        """Test that list_registries returns empty dict when no registries exist."""
        _, config_path = temp_config_dir
        result = list_registries()
        assert result == {}

    def test_returns_registries_when_exist(self, temp_config_dir):
        """Test that list_registries returns all registries."""
        _, config_path = temp_config_dir
        add_registry("local", "http://localhost:8000")
        add_registry("central", "https://api.stardag.com")

        result = list_registries()
        assert result == {
            "local": "http://localhost:8000",
            "central": "https://api.stardag.com",
        }


class TestAddRegistry:
    def test_creates_registry_entry(self, temp_config_dir):
        """Test that add_registry creates a registry entry in config."""
        _, config_path = temp_config_dir
        add_registry("test-registry", "http://localhost:8000")

        result = list_registries()
        assert "test-registry" in result
        assert result["test-registry"] == "http://localhost:8000"

    def test_strips_trailing_slash_from_url(self, temp_config_dir):
        """Test that add_registry strips trailing slashes from URL."""
        _, config_path = temp_config_dir
        add_registry("test-registry", "http://api.example.com/")

        result = list_registries()
        assert result["test-registry"] == "http://api.example.com"


class TestRemoveRegistry:
    def test_removes_existing_registry(self, temp_config_dir):
        """Test that remove_registry removes the registry."""
        _, config_path = temp_config_dir
        add_registry("to-delete", "http://localhost:8000")

        result = remove_registry("to-delete")

        assert result is True
        assert "to-delete" not in list_registries()

    def test_returns_false_for_nonexistent_registry(self, temp_config_dir):
        """Test that remove_registry returns False for nonexistent registry."""
        _, config_path = temp_config_dir
        result = remove_registry("nonexistent")
        assert result is False
