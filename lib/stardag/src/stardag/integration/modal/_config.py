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
    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    result = list(pyproject["project"]["dependencies"])
    result += pyproject["project"].get("optional-dependencies", {}).get("modal", [])
    if include_dev_deps:
        result += pyproject.get("dependency-groups", {}).get("dev", [])
    return result
