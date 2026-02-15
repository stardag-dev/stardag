"""Tests for stardag.integration.modal._config module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.integration.modal._config import (
    ModalConfig,
    _get_stardag_deps_for_image,
    get_package_deps,
    modal_config_provider,
    with_stardag_on_image,
)


class TestModalConfig:
    """Tests for ModalConfig settings class."""

    def test_default_values(self) -> None:
        """Test ModalConfig has correct defaults."""
        config = ModalConfig()
        assert config.volume_mounts == {}
        assert config.local_stardag_source == "auto"

    def test_volume_mounts_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test volume_mounts can be set via environment variable."""
        monkeypatch.setenv(
            "STARDAG_MODAL_VOLUME_MOUNTS",
            '{"//data": "my-volume", "//cache": "cache-vol"}',
        )
        config = ModalConfig()
        assert config.volume_mounts == {
            "//data": "my-volume",
            "//cache": "cache-vol",
        }

    def test_local_stardag_source_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test local_stardag_source can be set via environment variable."""
        monkeypatch.setenv("STARDAG_MODAL_LOCAL_STARDAG_SOURCE", "yes")
        config = ModalConfig()
        assert config.local_stardag_source == "yes"

        monkeypatch.setenv("STARDAG_MODAL_LOCAL_STARDAG_SOURCE", "no")
        config = ModalConfig()
        assert config.local_stardag_source == "no"

    def test_volume_name_to_mount_path_property(self) -> None:
        """Test volume_name_to_mount_path property inverts and normalizes paths."""
        config = ModalConfig(
            volume_mounts={
                "/data/": "data-volume",
                "/cache": "cache-volume",
            }
        )
        result = config.volume_name_to_mount_path
        assert result == {
            "data-volume": Path("/data"),
            "cache-volume": Path("/cache"),
        }

    def test_volume_name_to_mount_path_empty(self) -> None:
        """Test volume_name_to_mount_path with no mounts."""
        config = ModalConfig(volume_mounts={})
        assert config.volume_name_to_mount_path == {}


class TestGetPackageDeps:
    """Tests for get_package_deps function."""

    def test_reads_project_dependencies(self, tmp_path: Path) -> None:
        """Test reading basic project dependencies."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = [
    "pydantic>=2.0",
    "httpx>=0.27.0",
]
""")
        deps = get_package_deps(pyproject)
        assert deps == ["pydantic>=2.0", "httpx>=0.27.0"]

    def test_includes_dependency_groups(self, tmp_path: Path) -> None:
        """Test including dependency groups."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.1.0"]
""")
        deps = get_package_deps(pyproject, groups=["dev"])
        assert deps == ["pydantic>=2.0", "pytest>=8.0", "ruff>=0.1.0"]

    def test_includes_optional_dependencies(self, tmp_path: Path) -> None:
        """Test including optional dependencies."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]

[project.optional-dependencies]
modal = ["modal>=1.0.0"]
s3 = ["boto3>=1.34"]
""")
        deps = get_package_deps(pyproject, optional=["modal", "s3"])
        assert deps == ["pydantic>=2.0", "modal>=1.0.0", "boto3>=1.34"]

    def test_combines_groups_and_optional(self, tmp_path: Path) -> None:
        """Test combining dependency groups and optional dependencies."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]

[dependency-groups]
dev = ["pytest>=8.0"]

[project.optional-dependencies]
modal = ["modal>=1.0.0"]
""")
        deps = get_package_deps(pyproject, groups=["dev"], optional=["modal"])
        assert deps == ["pydantic>=2.0", "pytest>=8.0", "modal>=1.0.0"]

    def test_finds_pyproject_in_parent_directory(self, tmp_path: Path) -> None:
        """Test finding pyproject.toml in parent directories."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]
""")
        subdir = tmp_path / "src" / "mypackage"
        subdir.mkdir(parents=True)
        some_file = subdir / "module.py"
        some_file.write_text("# some code")

        deps = get_package_deps(some_file)
        assert deps == ["pydantic>=2.0"]

    def test_raises_on_missing_pyproject(self, tmp_path: Path) -> None:
        """Test raises FileNotFoundError when pyproject.toml not found."""
        # Create a directory structure with no pyproject.toml
        # and use it as a starting point for the search
        isolated_dir = tmp_path / "isolated" / "subdir"
        isolated_dir.mkdir(parents=True)
        some_file = isolated_dir / "module.py"
        some_file.write_text("# code")

        with pytest.raises(FileNotFoundError, match="Could not find pyproject.toml"):
            get_package_deps(some_file)

    def test_raises_on_missing_group(self, tmp_path: Path) -> None:
        """Test raises ValueError for missing dependency group."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]
""")
        with pytest.raises(
            ValueError, match="Dependency group 'nonexistent' not found"
        ):
            get_package_deps(pyproject, groups=["nonexistent"])

    def test_raises_on_missing_optional(self, tmp_path: Path) -> None:
        """Test raises ValueError for missing optional dependency."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = ["pydantic>=2.0"]
""")
        with pytest.raises(
            ValueError, match="Optional dependency 'nonexistent' not found"
        ):
            get_package_deps(pyproject, optional=["nonexistent"])

    def test_works_with_actual_pyproject(self) -> None:
        """Test get_package_deps works with the actual stardag pyproject.toml."""
        pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
        if not pyproject_path.exists():
            pytest.skip("pyproject.toml not found")

        deps = get_package_deps(pyproject_path)
        # Should include core dependencies
        assert any("pydantic" in d for d in deps)
        assert any("httpx" in d for d in deps)

        # Test with optional modal deps
        deps_with_modal = get_package_deps(pyproject_path, optional=["modal"])
        assert any("modal" in d for d in deps_with_modal)


class TestGetStardagDepsForImage:
    """Tests for _get_stardag_deps_for_image function."""

    def test_returns_deps_with_modal_optional(self) -> None:
        """Test it returns deps including modal optional dependency."""
        deps = _get_stardag_deps_for_image(include_dev_deps=False)
        # Should include modal optional deps
        assert any("modal" in d for d in deps)
        # Should include core deps
        assert any("pydantic" in d for d in deps)

    def test_includes_dev_deps_when_requested(self) -> None:
        """Test it includes dev dependencies when include_dev_deps=True."""
        deps_without_dev = _get_stardag_deps_for_image(include_dev_deps=False)
        deps_with_dev = _get_stardag_deps_for_image(include_dev_deps=True)

        # Dev deps should have more entries
        assert len(deps_with_dev) > len(deps_without_dev)
        # Should include pytest from dev deps
        assert any("pytest" in d for d in deps_with_dev)

    def test_returns_empty_list_when_pyproject_missing(self) -> None:
        """Test it returns empty list when pyproject.toml doesn't exist."""
        # Mock Path to return a path that doesn't exist
        with patch("stardag.integration.modal._config.Path") as mock_path_class:
            # Create a mock that mimics Path behavior
            mock_pyproject_path = MagicMock()
            mock_pyproject_path.exists.return_value = False

            # Set up the chain: Path(__file__).parents[4] / "pyproject.toml"
            mock_file_path = MagicMock()
            mock_parents = MagicMock()
            mock_parents.__getitem__.return_value.__truediv__.return_value = (
                mock_pyproject_path
            )
            mock_file_path.parents = mock_parents
            mock_path_class.return_value = mock_file_path

            deps = _get_stardag_deps_for_image()

        assert deps == []


class TestWithStardagOnImage:
    """Tests for with_stardag_on_image function."""

    def test_uses_pypi_for_release_version(self) -> None:
        """Test it installs from PyPI for release versions."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image

        with patch("stardag.integration.modal._config.sd") as mock_sd:
            mock_sd.__version__ = "1.0.0"

            result = with_stardag_on_image(mock_image)

        mock_image.pip_install.assert_called_once_with("stardag[modal]==1.0.0")
        assert result == mock_image

    def test_uses_pypi_with_explicit_version(self) -> None:
        """Test it uses explicit version when provided."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image

        result = with_stardag_on_image(mock_image, version="2.0.0")

        mock_image.pip_install.assert_called_once_with("stardag[modal]==2.0.0")
        assert result == mock_image

    def test_uses_local_source_for_dev_version(self) -> None:
        """Test it uses local source for dev versions (containing 'dev')."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image
        mock_image.add_local_python_source.return_value = mock_image

        with (
            patch("stardag.integration.modal._config.sd") as mock_sd,
            patch.object(
                modal_config_provider,
                "get",
                return_value=ModalConfig(local_stardag_source="auto"),
            ),
            patch(
                "stardag.integration.modal._config._get_stardag_deps_for_image",
                return_value=["pydantic>=2.0", "modal>=1.0"],
            ),
        ):
            mock_sd.__version__ = "1.0.1.dev3"

            result = with_stardag_on_image(mock_image)

        mock_image.pip_install.assert_called_once_with("pydantic>=2.0", "modal>=1.0")
        mock_image.add_local_python_source.assert_called_once_with("stardag")
        assert result == mock_image

    def test_uses_local_source_for_version_with_plus(self) -> None:
        """Test it uses local source for versions with '+' (git hash)."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image
        mock_image.add_local_python_source.return_value = mock_image

        with (
            patch("stardag.integration.modal._config.sd") as mock_sd,
            patch.object(
                modal_config_provider,
                "get",
                return_value=ModalConfig(local_stardag_source="auto"),
            ),
            patch(
                "stardag.integration.modal._config._get_stardag_deps_for_image",
                return_value=["pydantic>=2.0"],
            ),
        ):
            mock_sd.__version__ = "1.0.0+g389c509a7"

            result = with_stardag_on_image(mock_image)

        mock_image.pip_install.assert_called_once_with("pydantic>=2.0")
        mock_image.add_local_python_source.assert_called_once_with("stardag")
        assert result == mock_image

    def test_local_source_yes_overrides_release_version(self) -> None:
        """Test local_stardag_source='yes' forces local source even for release."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image
        mock_image.add_local_python_source.return_value = mock_image

        with (
            patch("stardag.integration.modal._config.sd") as mock_sd,
            patch.object(
                modal_config_provider,
                "get",
                return_value=ModalConfig(local_stardag_source="yes"),
            ),
            patch(
                "stardag.integration.modal._config._get_stardag_deps_for_image",
                return_value=["pydantic>=2.0"],
            ),
        ):
            mock_sd.__version__ = "1.0.0"  # Release version

            result = with_stardag_on_image(mock_image)

        # Should still use local source
        mock_image.add_local_python_source.assert_called_once_with("stardag")
        assert result == mock_image

    def test_local_source_no_forces_pypi_for_dev_version(self) -> None:
        """Test local_stardag_source='no' forces PyPI even for dev version."""
        mock_image = MagicMock(spec=modal.Image)
        mock_image.pip_install.return_value = mock_image

        with (
            patch("stardag.integration.modal._config.sd") as mock_sd,
            patch.object(
                modal_config_provider,
                "get",
                return_value=ModalConfig(local_stardag_source="no"),
            ),
        ):
            mock_sd.__version__ = "1.0.1.dev3"  # Dev version

            result = with_stardag_on_image(mock_image)

        # Should use PyPI despite dev version
        mock_image.pip_install.assert_called_once_with("stardag[modal]==1.0.1.dev3")
        mock_image.add_local_python_source.assert_not_called()
        assert result == mock_image
