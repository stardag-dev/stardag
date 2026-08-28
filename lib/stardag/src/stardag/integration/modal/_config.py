import json
import logging
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Literal

import modal
import tomllib
from pydantic_settings import BaseSettings, SettingsConfigDict

import stardag as sd
from stardag.utils.resource_provider import resource_provider

logger = logging.getLogger(__name__)


class ModalConfig(BaseSettings):
    """Configuration of the modal integration."""

    volume_mounts: dict[str, str] = {}  # path -> volume name

    # Where a Modal image gets stardag from: the local working tree, or
    # PyPI. ``auto`` ships the working tree when stardag is running from
    # one (an editable install, or a dev build) and installs the pinned
    # release otherwise. See :func:`with_stardag_on_image` for why the
    # choice matters more than it looks.
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


def _running_from_editable_install() -> bool:
    """Whether the imported ``stardag`` is an editable (or bare source) install.

    This is the question ``local_stardag_source="auto"`` actually wants
    answered — "am I running stardag from a working tree?" — and it is
    answerable directly, from the installer's own record.

    It used to be inferred from the version string instead, which is wrong
    for exactly this case: ``stardag.__version__`` is
    ``importlib.metadata.version("stardag")``, and an editable install's
    metadata version is a **snapshot taken when the install ran**. hatch-vcs
    computes it from the git tag reachable at that moment and nothing
    recomputes it as the working tree moves on — not even a plain
    ``uv sync``, which sees the editable install already present and leaves
    its metadata alone. So a checkout installed at v0.17.0 keeps reporting
    ``0.17.0`` while its source is v0.20.x. That string carries no ``dev``
    and no ``+``, so it read as a plain released version — and the image was
    pinned to a **real PyPI release that predates the source being pickled
    into it**. See :func:`with_stardag_on_image` for what that does.

    ``PackageNotFoundError`` means stardag is importable but not installed
    at all (a bare ``sys.path`` entry), which is a working tree by any other
    name and wants the same treatment.
    """
    try:
        direct_url = distribution("stardag").read_text("direct_url.json")
    except PackageNotFoundError:
        return True
    if not direct_url:
        return False
    try:
        return bool(json.loads(direct_url).get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError):
        # Malformed record: not evidence of an editable install.
        return False


def with_stardag_on_image(
    image: modal.Image,
    version: str | None = None,
) -> modal.Image:
    """Make stardag available in the given Modal image.

    Two ways, chosen by ``ModalConfig.local_stardag_source``
    (``STARDAG_MODAL_LOCAL_STARDAG_SOURCE``):

    - **From PyPI**, pinned to the running version. The default for an
      ordinary installed stardag.
    - **From the local working tree**, via ``add_local_python_source``, plus
      stardag's own dependencies from its ``pyproject.toml``. The default
      when stardag is installed editable or is a dev build — i.e. when you
      are developing stardag itself.

    Getting that choice wrong is not a slow path, it is a broken deploy.
    ``StardagApp.finalize`` registers ``serialized=True`` functions, and
    cloudpickle writes stardag's own callables out as *references* to their
    defining modules. If the image's stardag is older than the source that
    did the pickling, the app deploys cleanly and then every container dies
    at hydration with ``ModuleNotFoundError`` for a module the deploying
    process could see and the container cannot — before any of the app's own
    code runs. Which is why ``auto`` asks the installer whether this is a
    working tree (see :func:`_running_from_editable_install`) rather than
    guessing from a version string that an editable install freezes at
    install time.

    Args:
        image: The Modal image to install stardag into.
        version: Pin the PyPI install to this version instead of the running
            one. Ignored when the local source is used. Pinning a version
            **older than the source doing the pickling** reintroduces the
            failure above, so it is warned about — and warned about
            differently from a working tree, where there is no trustworthy
            running version to compare the pin against at all.
    Returns:
        The updated Modal image.
    """
    running_version = sd.__version__
    explicitly_pinned = bool(version)
    pinned_version = version or running_version
    # A dev build's version says so: "0.1.1.dev3+g389c509a7".
    is_dev_version = "dev" in running_version or "+" in running_version
    local_stardag_source = modal_config_provider.get().local_stardag_source

    from_working_tree = is_dev_version or _running_from_editable_install()
    use_local_stardag_source = local_stardag_source == "yes" or (
        local_stardag_source == "auto" and from_working_tree
    )

    if use_local_stardag_source:
        sd_deps = _get_stardag_deps_for_image(include_dev_deps=False)
        return image.pip_install(*sd_deps).add_local_python_source("stardag")

    # Pinning to PyPI. Say so when the pin cannot be trusted to match the
    # source about to be pickled into this image — a warning rather than a
    # refusal, because both routes here are something the caller asked for
    # explicitly, and the pinned release may well be the right one.
    if from_working_tree and explicitly_pinned:
        logger.warning(
            "Pinning stardag==%s from PyPI into a Modal image while running "
            "stardag from a working tree. There is no version to check that "
            "pin against — an editable install's recorded version is frozen "
            "at install time, so the tree's real version is unknown — and if "
            "the pin is older than the code being serialized into this app's "
            "functions, the app deploys cleanly and every container then "
            "fails to hydrate. Drop the `version=` argument and set "
            "STARDAG_MODAL_LOCAL_STARDAG_SOURCE=yes to ship the working tree "
            "instead.",
            pinned_version,
        )
    elif from_working_tree:
        logger.warning(
            "Installing stardag==%s from PyPI into a Modal image while "
            "running stardag from a working tree. That version is read from "
            "the install metadata, which an editable install freezes at "
            "install time — so it may be older than the code being "
            "serialized into this app's functions, in which case the app "
            "deploys cleanly and every container then fails to hydrate. "
            "Set STARDAG_MODAL_LOCAL_STARDAG_SOURCE=yes to ship the working "
            "tree instead, or refresh the recorded version (a plain "
            "`uv sync` will not: the install is already there, so nothing "
            "rebuilds its metadata — use "
            "`uv sync --reinstall-package stardag`).",
            pinned_version,
        )
    elif pinned_version != running_version:
        logger.warning(
            "Installing stardag==%s from PyPI into a Modal image while "
            "running %s. Functions registered by StardagApp are serialized "
            "and reference stardag's own modules by name, so a pinned "
            "version older than the running one can leave every container "
            "unable to hydrate.",
            pinned_version,
            running_version,
        )
    return image.pip_install(f"stardag[modal]=={pinned_version}")


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
