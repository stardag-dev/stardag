from pathlib import Path
from typing import Literal

import modal
import tomllib
from pydantic_settings import BaseSettings, SettingsConfigDict

import stardag as sd
from stardag.utils.resource_provider import resource_provider


class ModalConfig(BaseSettings):
    """Configuration of the modal integration."""

    volume_mounts: dict[str, str] = {}  # path -> volume name

    local_stardag_source: Literal["yes", "no", "auto"] = "auto"

    model_config = SettingsConfigDict(
        env_prefix="stardag_modal_",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
    )

    @property
    def volume_name_to_mount_path(self) -> dict[str, Path]:
        return {v: Path(p.removesuffix("/")) for p, v in self.volume_mounts.items()}


modal_config_provider = resource_provider(ModalConfig, ModalConfig)


def with_stardag_on_image(
    image: modal.Image,
    version: str | None = None,
) -> modal.Image:
    """Install the latest version of stardag from PyPI into the given Modal image.

    Args:
        image: The Modal image to install stardag into.
        version: The version of stardag to install. If None, installs the latest
            version.
    Returns:
        The updated Modal image.
    """
    version = version or sd.__version__
    # if on a dev version: "0.1.1.dev3+g389c509a7"
    is_dev_version = "dev" in version or "+" in version
    # if we are on a dev version, install from local source
    local_stardag_source = modal_config_provider.get().local_stardag_source

    use_local_stardag_source = (
        is_dev_version and local_stardag_source != "no"
    ) or local_stardag_source == "yes"

    if use_local_stardag_source:
        sd_deps = _get_stardag_deps_for_image(include_dev_deps=True)
        return image.pip_install(*sd_deps).add_local_python_source("stardag")
    else:
        return image.pip_install(f"stardag[modal]=={version}")


def _get_stardag_deps_for_image(include_dev_deps: bool = False) -> list[str]:
    """Extract dependencies from pyproject.toml for Modal image.

    Returns empty list when running inside Modal (pyproject.toml not available),
    since deps are already installed in the image at that point.
    """
    pyproject_path = Path(__file__).parents[4] / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    return get_package_deps(
        pyproject_path=pyproject_path,
        groups=["dev"] if include_dev_deps else None,
        optional=["modal"],
    )


def get_package_deps(
    pyproject_path: Path | str,
    *,
    groups: list[str] | None = None,
    optional: list[str] | None = None,
) -> list[str]:
    """Extract dependencies from pyproject.toml.

    Args:
        pyproject_path: The path to the pyproject.toml file or if any other file it
            looks for first pyproject.toml in the parent directories.
        groups: The dependency groups to include (e.g. ["dev"]). If None, does not
            include any groups.
        optional: The optional dependencies to include (e.g. ["modal"]). If None, does
            not include any optional dependencies.

    Returns:
        A list of dependencies, with version specifiers.

    Raises:
        FileNotFoundError: If the pyproject.toml file is not found.
        ValueError: If a specified dependency group or optional dependency is not found.
    """
    pyproject_path = Path(pyproject_path)
    if pyproject_path.name != "pyproject.toml":
        # look for first pyproject.toml in parent directories
        current_path = (
            pyproject_path.parent if pyproject_path.is_file() else pyproject_path
        )
        while True:
            pyproject_path = current_path / "pyproject.toml"
            if pyproject_path.exists():
                break
            if current_path.parent == current_path:
                raise FileNotFoundError("Could not find pyproject.toml")
            current_path = current_path.parent

    groups = groups or []
    optional = optional or []

    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    result = list(pyproject["project"]["dependencies"])
    for group in groups:
        try:
            result += pyproject.get("dependency-groups", {})[group]
        except KeyError:
            raise ValueError(f"Dependency group '{group}' not found in pyproject.toml")
    for opt in optional:
        try:
            result += pyproject["project"].get("optional-dependencies", {})[opt]
        except KeyError:
            raise ValueError(f"Optional dependency '{opt}' not found in pyproject.toml")

    return result
