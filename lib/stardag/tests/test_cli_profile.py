"""Tests for CLI profile management commands."""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from stardag.cli.credentials import (
    create_profile,
    delete_profile,
    list_profiles,
)


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory for stardag config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        profiles_dir = tmppath / "profiles"
        profiles_dir.mkdir()

        # Patch the config module path functions
        with mock.patch("stardag.config.get_stardag_dir", return_value=tmppath):
            with mock.patch(
                "stardag.config.get_profiles_dir", return_value=profiles_dir
            ):
                with mock.patch(
                    "stardag.config.get_profile_dir",
                    side_effect=lambda p: profiles_dir / p,
                ):
                    with mock.patch(
                        "stardag.config.get_profile_config_path",
                        side_effect=lambda p: profiles_dir / p / "config.json",
                    ):
                        with mock.patch(
                            "stardag.config.get_profile_credentials_path",
                            side_effect=lambda p: profiles_dir / p / "credentials.json",
                        ):
                            with mock.patch(
                                "stardag.config.get_active_profile_path",
                                return_value=tmppath / "active_profile",
                            ):
                                with mock.patch(
                                    "stardag.config.load_active_profile",
                                    return_value="default",
                                ):
                                    yield tmppath, profiles_dir


class TestListProfiles:
    def test_returns_empty_when_no_profiles(self, temp_config_dir):
        """Test that list_profiles returns empty list when no profiles exist."""
        _, profiles_dir = temp_config_dir
        result = list_profiles()
        assert result == []

    def test_returns_profiles_when_exist(self, temp_config_dir):
        """Test that list_profiles returns all profile directories."""
        _, profiles_dir = temp_config_dir
        (profiles_dir / "local").mkdir()
        (profiles_dir / "central").mkdir()

        result = list_profiles()
        assert set(result) == {"local", "central"}


class TestCreateProfile:
    def test_creates_profile_directory(self, temp_config_dir):
        """Test that create_profile creates the profile directory."""
        _, profiles_dir = temp_config_dir
        create_profile("test-profile", "http://localhost:8000")

        profile_dir = profiles_dir / "test-profile"
        assert profile_dir.exists()

    def test_creates_config_with_api_url(self, temp_config_dir):
        """Test that create_profile saves the API URL."""
        _, profiles_dir = temp_config_dir
        create_profile("test-profile", "http://api.example.com")

        config_path = profiles_dir / "test-profile" / "config.json"
        assert config_path.exists()

        with open(config_path) as f:
            config = json.load(f)
        assert config["api_url"] == "http://api.example.com"


class TestDeleteProfile:
    def test_deletes_existing_profile(self, temp_config_dir):
        """Test that delete_profile removes the profile directory."""
        _, profiles_dir = temp_config_dir
        profile_dir = profiles_dir / "to-delete"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.json").write_text("{}")

        result = delete_profile("to-delete")

        assert result is True
        assert not profile_dir.exists()

    def test_returns_false_for_nonexistent_profile(self, temp_config_dir):
        """Test that delete_profile returns False for nonexistent profile."""
        result = delete_profile("nonexistent")
        assert result is False
