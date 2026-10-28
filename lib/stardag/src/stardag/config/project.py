"""Package settings: ``[tool.stardag]`` in a package's ``pyproject.toml``.

Settings that describe a *code base* rather than a registry profile, kept
apart from registry configuration (``.stardag/config.toml`` and the user
config are never read for them) and read even with ``STARDAG_NO_REGISTRY=1``::

    # <package>/pyproject.toml
    [tool.stardag]
    task_store = "src/my_package/stardag_tasks"   # or "pkg:my_package/stardag_tasks"
    task_modules = ["my_package.tasks.*", "!my_package.tasks.deploy"]

- ``task_store``: the committed task store (``MyTask.from_store``,
  ``sd.write_to_store``, ``stardag tasks ref --write``). A relative path
  resolves against the directory of the ``pyproject.toml`` declaring it. A
  ``pkg:<package>/<path>`` spec names a directory inside an importable
  package (``importlib.resources``), which ships with the package: it
  resolves the same from a checkout, an installed wheel and a container
  image.
- ``task_modules``: modules whose import registers the package's task
  classes, in the pattern grammar of ``StardagApp(task_modules=...)``
  (``"pkg.mod"``, ``"pkg.tasks.*"``, ``"!pkg.tasks.deploy"``). Module
  names, resolved by the import system: no path context.

**Which package's settings apply** is decided per call, not per process,
so in a repository of several packages each one's pins read its own store
(see :func:`resolve_project_config`). In order:

1. ``STARDAG_TASK_STORE`` / ``STARDAG_TASK_MODULES`` (env), when set; a
   relative env path resolves against the working directory;
2. walking up from each *anchor* file, in order, to the nearest
   ``pyproject.toml`` that has a ``[tool.stardag]`` table (a
   ``pyproject.toml`` without one, a monorepo root's, is passed). The walk
   never leaves an installed package: a file under ``site-packages`` /
   ``dist-packages`` is resolved inside its top-level package, where the
   store is the one directory holding a ``stardag_task_store.toml`` marker
   (written by every store writer);
3. the same walk from the working directory.

The anchors are, for ``from_store`` / ``write_to_store``, the calling
module's file and then the task class's defining module; for a reference,
the file of the code that created it and then the referenced class's
module (:mod:`stardag._core.anchor`).
"""

from __future__ import annotations

import importlib.resources
import os
import threading
import tomllib
import typing
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

TOOL_TABLE = "stardag"
TASK_STORE_ENV = "STARDAG_TASK_STORE"
TASK_MODULES_ENV = "STARDAG_TASK_MODULES"
#: The file marking a store directory, so an installed package's store is
#: found without its ``pyproject.toml`` (which a wheel does not ship).
STORE_MARKER = "stardag_task_store.toml"
PKG_PREFIX = "pkg:"
_SITE_DIRS = ("site-packages", "dist-packages")
# How deep inside an installed top-level package a store marker is looked for.
_MARKER_MAX_DEPTH = 4


class ProjectConfigError(ValueError):
    """A ``[tool.stardag]`` table or store spec is malformed or ambiguous."""


class ProjectConfig(BaseModel):
    """The settings that apply to one call.

    Attributes:
        config_path: The ``pyproject.toml`` (or, for an installed package,
            the store marker) they were read from; ``None`` when none.
        root: The directory relative store paths resolve against: the
            ``pyproject.toml``'s directory (an installed package's top-level
            package directory); ``None`` when no config was found.
        task_store: The store directory, absolute; ``None`` when not set.
        task_modules: Task-module patterns (validated where expanded).
    """

    config_path: Path | None = None
    root: Path | None = None
    task_store: Path | None = None
    task_modules: tuple[str, ...] = ()


# -----------------------------------------------------------------------------
# Store specs
# -----------------------------------------------------------------------------


def resolve_store_spec(spec: str | os.PathLike[str], base: Path | None) -> Path:
    """An absolute store directory for ``spec``: ``pkg:<package>/<path>``
    (a directory inside an importable package), or a path, relative ones
    against ``base`` (the working directory when ``None``)."""
    text = os.fspath(spec)
    if text.startswith(PKG_PREFIX):
        package, _, inner = text[len(PKG_PREFIX) :].partition("/")
        if not package:
            raise ProjectConfigError(
                f"Malformed store spec {text!r}: expected "
                '"pkg:<package>/<path inside it>", e.g. "pkg:my_package/stardag_tasks".'
            )
        try:
            files = importlib.resources.files(package)
        except (ModuleNotFoundError, TypeError) as e:
            raise ProjectConfigError(
                f"Store spec {text!r}: package {package!r} cannot be imported ({e})."
            ) from e
        target = files.joinpath(*[p for p in inner.split("/") if p])
        return Path(str(target)).absolute()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.absolute()


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_DIR_CACHE: dict[Path, ProjectConfig | None] = {}


def clear_project_config_cache() -> None:
    """Forget discovered settings (after editing a ``pyproject.toml``)."""
    with _CACHE_LOCK:
        _DIR_CACHE.clear()


def _read_tool_table(pyproject: Path) -> dict[str, typing.Any] | None:
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get("tool", {}).get(TOOL_TABLE)
    return table if isinstance(table, dict) else None


def _from_table(table: dict[str, typing.Any], pyproject: Path) -> ProjectConfig:
    root = pyproject.parent
    store = table.get("task_store")
    modules = table.get("task_modules", ())
    if isinstance(modules, str):
        modules = [modules]
    if not isinstance(modules, (list, tuple)):
        raise ProjectConfigError(
            f"{pyproject}: [tool.stardag] task_modules must be a list of patterns."
        )
    if store is not None and not isinstance(store, str):
        raise ProjectConfigError(
            f"{pyproject}: [tool.stardag] task_store must be a string."
        )
    return ProjectConfig(
        config_path=pyproject,
        root=root,
        task_store=resolve_store_spec(store, root) if store else None,
        task_modules=tuple(str(m) for m in modules),
    )


def _installed_package_dir(directory: Path) -> Path | None:
    """The top-level package directory when ``directory`` is inside an
    installed package (under ``site-packages`` / ``dist-packages``)."""
    parts = directory.parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in _SITE_DIRS:
            return Path(*parts[: i + 2]) if i + 1 < len(parts) else None
    return None


def _installed_config(package_dir: Path) -> ProjectConfig | None:
    markers: list[Path] = []
    for current, dirs, files in os.walk(package_dir):
        depth = len(Path(current).relative_to(package_dir).parts)
        if STORE_MARKER in files:
            markers.append(Path(current) / STORE_MARKER)
        if depth >= _MARKER_MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith((".", "__pycache__"))]
    if not markers:
        return None
    if len(markers) > 1:
        listed = ", ".join(str(m.parent) for m in sorted(markers))
        raise ProjectConfigError(
            f"The installed package {package_dir} holds several task stores "
            f"({listed}); pass store=... to pick one."
        )
    return ProjectConfig(
        config_path=markers[0], root=package_dir, task_store=markers[0].parent
    )


def discover_project_config(start: Path) -> ProjectConfig | None:
    """The settings found walking up from directory ``start`` (see the
    module docstring), or ``None``. Cached per directory."""
    start = start.absolute()
    with _CACHE_LOCK:
        if start in _DIR_CACHE:
            return _DIR_CACHE[start]
    installed = _installed_package_dir(start)
    found: ProjectConfig | None = None
    if installed is not None:
        found = _installed_config(installed)
    else:
        for directory in (start, *start.parents):
            if directory.name in _SITE_DIRS:
                break
            pyproject = directory / "pyproject.toml"
            if pyproject.is_file():
                table = _read_tool_table(pyproject)
                if table is not None:
                    found = _from_table(table, pyproject)
                    break
    with _CACHE_LOCK:
        _DIR_CACHE[start] = found
    return found


# -----------------------------------------------------------------------------
# Resolution (with a test override)
# -----------------------------------------------------------------------------

_OVERRIDES: list[ProjectConfig | None] = []
_OVERRIDE_LOCK = threading.Lock()


@contextmanager
def project_config_override(
    config: ProjectConfig | None,
) -> typing.Iterator[None]:
    """Use ``config`` for every call in the block, instead of discovery and
    env vars (tests). ``None`` re-enables discovery inside an outer
    override."""
    with _OVERRIDE_LOCK:
        _OVERRIDES.append(config)
    try:
        yield
    finally:
        with _OVERRIDE_LOCK:
            _OVERRIDES.pop()


def resolve_project_config(
    anchors: typing.Iterable[Path | None] = (),
) -> ProjectConfig:
    """The settings for a call anchored at ``anchors`` (files or directories,
    tried in order), else the working directory, with env overrides."""
    with _OVERRIDE_LOCK:
        if _OVERRIDES and _OVERRIDES[-1] is not None:
            return _OVERRIDES[-1]
    found: ProjectConfig | None = None
    for anchor in anchors:
        if anchor is None:
            continue
        directory = anchor if anchor.is_dir() else anchor.parent
        found = discover_project_config(directory)
        if found is not None:
            break
    if found is None:
        found = discover_project_config(Path.cwd()) or ProjectConfig()
    env_store = os.environ.get(TASK_STORE_ENV)
    env_modules = os.environ.get(TASK_MODULES_ENV)
    if env_store or env_modules is not None:
        found = found.model_copy(
            update={
                **(
                    {"task_store": resolve_store_spec(env_store, None)}
                    if env_store
                    else {}
                ),
                **(
                    {
                        "task_modules": tuple(
                            p.strip() for p in env_modules.split(",") if p.strip()
                        )
                    }
                    if env_modules is not None
                    else {}
                ),
            }
        )
    return found
