"""Project settings: the ``[project]`` table of ``.stardag/config.toml``.

Settings that describe a *code base* rather than a registry profile, so they
live in the project config (committed with the code) and are read even with
``STARDAG_NO_REGISTRY=1``, which an offline test suite sets::

    # <repo>/.stardag/config.toml
    [project]
    task_store = "stardag-tasks"              # the committed task store
    task_modules = ["my_project.tasks.*"]     # where the task classes live

- ``task_store``: the directory of the committed task store
  (``MyTask.from_store``, ``sd.write_to_store``, ``stardag tasks ref
  --write``). A relative path resolves against the **project root**, the
  directory holding ``.stardag/``, never the working directory, so the
  same config works from any subdirectory.
- ``task_modules``: modules whose import registers the project's task
  classes, in the pattern grammar of ``StardagApp(task_modules=...)`` (an
  exact module, or a trailing recursive wildcard ``"pkg.tasks.*"``).
  Imported lazily, once per process, the first time a stored body names a
  task class that is not registered.

Environment variables override the file: ``STARDAG_TASK_STORE`` (a relative
path resolves against the working directory, as for any path given on the
command line) and ``STARDAG_TASK_MODULES`` (comma-separated patterns).

Only the project config is read for this table: a user-level
``~/.stardag/config.toml`` has no project to describe.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from stardag.config.io import load_toml_file
from stardag.config.paths import find_project_config
from stardag.utils.resource_provider import resource_provider

#: The TOML table holding the project settings.
PROJECT_TABLE = "project"
TASK_STORE_ENV = "STARDAG_TASK_STORE"
TASK_MODULES_ENV = "STARDAG_TASK_MODULES"


class ProjectConfig(BaseModel):
    """The resolved project settings.

    Attributes:
        config_path: The project config file read, if one was found.
        task_store: The task store directory, absolute; ``None`` when not
            configured.
        task_modules: Task-module patterns (unvalidated here: they are
            validated where they are expanded).
    """

    config_path: Path | None = None
    task_store: Path | None = None
    task_modules: tuple[str, ...] = ()

    @property
    def root(self) -> Path | None:
        """The project root: the directory holding ``.stardag/``."""
        return self.config_path.parent.parent if self.config_path else None


def load_project_config() -> ProjectConfig:
    """Read the project settings: env vars over ``.stardag/config.toml``
    (found from the working directory upwards)."""
    config_path = find_project_config()
    table: dict[str, object] = {}
    if config_path is not None:
        raw = load_toml_file(config_path).get(PROJECT_TABLE, {})
        table = raw if isinstance(raw, dict) else {}

    task_store: Path | None = None
    env_store = os.environ.get(TASK_STORE_ENV)
    if env_store:
        task_store = Path(env_store).expanduser().absolute()
    else:
        configured = table.get("task_store")
        if isinstance(configured, str) and configured and config_path is not None:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = config_path.parent.parent / path
            task_store = path.absolute()

    task_modules: tuple[str, ...] = ()
    env_modules = os.environ.get(TASK_MODULES_ENV)
    if env_modules is not None:
        task_modules = tuple(p.strip() for p in env_modules.split(",") if p.strip())
    else:
        configured_modules = table.get("task_modules")
        if isinstance(configured_modules, str):
            configured_modules = [configured_modules]
        if isinstance(configured_modules, list):
            task_modules = tuple(str(p) for p in configured_modules)

    return ProjectConfig(
        config_path=config_path, task_store=task_store, task_modules=task_modules
    )


project_config_provider = resource_provider(
    ProjectConfig,
    default_factory=load_project_config,
    doc_str="The project settings (``[project]`` in .stardag/config.toml).",
)


def get_project_config() -> ProjectConfig:
    """The cached project settings (see :func:`load_project_config`)."""
    return project_config_provider.get()
