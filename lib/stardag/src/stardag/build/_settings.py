"""Build settings: the second half of the scope.

``settings`` is a flat ``dict[str, str]`` of environment variables applied
in every process of a build — the bootstrap, each tick, each worker and a
resident driver — chosen per trigger (``build_trigger(settings=...)``) or
per ``sd.build(settings=...)`` (design.md, "The deterministic scope", D4).

The contract, next to ``significant`` in the user docs: settings **may
change structure and execution, never output**. Anything that affects
output is a significant task parameter, because completion is global.
Read them at run time (the pydantic-settings pattern), not at import time:
a warm container imports before it knows its build.

Mechanics:

- Keys starting ``STARDAG_`` or ``MODAL_`` are refused (the framework's own
  configuration and identifiers), and values must be strings — checked
  here, at the trigger, before anything is created.
- Precedence where a key appears in several places: settings over the
  worker selector's per-task env over the deployment's env; the framework's
  identifiers (``STARDAG_PLAN_ID``, ``STARDAG_EXECUTION_ID``, ...) are
  written last by the executor and win over all three.
- Workers and ticks apply them in a scoped :func:`settings_applied` around
  the run; a resident driver applies them for the whole build with
  :func:`resident_settings`, which refuses a second concurrent build in the
  same process under different settings (the environment is per process).
"""

from __future__ import annotations

import contextlib
import os
import threading
import typing
from collections.abc import Mapping

from stardag.exceptions import StardagError
from stardag.utils.env import temp_env_vars

RESERVED_SETTINGS_PREFIXES = ("STARDAG_", "MODAL_")

Settings = dict[str, str]


class SettingsError(StardagError, ValueError):
    """Settings that cannot be applied: a reserved key, a non-string value,
    or a second resident build in this process under different settings."""


def validate_settings(settings: Mapping[str, object] | None) -> Settings:
    """A validated, plain copy of ``settings`` (``{}`` for None).

    Raises:
        SettingsError: A key or value is not a string, or a key is reserved
            (``STARDAG_*`` / ``MODAL_*``).
    """
    checked: Settings = {}
    for key, value in (settings or {}).items():
        if not isinstance(key, str) or not key:
            raise SettingsError(f"settings keys are non-empty strings, got {key!r}")
        if key.startswith(RESERVED_SETTINGS_PREFIXES):
            raise SettingsError(
                f"settings key {key!r} is reserved: keys starting "
                f"{' or '.join(RESERVED_SETTINGS_PREFIXES)} are the framework's "
                "own configuration and cannot be set per build."
            )
        if not isinstance(value, str):
            raise SettingsError(
                f"settings[{key!r}] must be a string (settings are environment "
                f"variables), got {type(value).__name__}"
            )
        checked[key] = value
    return checked


@contextlib.contextmanager
def settings_applied(settings: Mapping[str, str] | None) -> typing.Iterator[None]:
    """Apply ``settings`` as environment variables for the block, restoring
    the previous values after (a tick's pass, a worker's run)."""
    with temp_env_vars(dict(settings or {})):
        yield


_resident_lock = threading.Lock()
# The settings every running resident build in this process was started
# with (all equal), and the environment values they replaced.
_resident_active: list[Settings] = []
_resident_restore: dict[str, str | None] = {}


@contextlib.contextmanager
def resident_settings(settings: Mapping[str, str] | None) -> typing.Iterator[None]:
    """Apply ``settings`` for a resident build's whole duration.

    The environment is per process, so two resident builds running at once
    in one process can only share it if they agree: a second one with
    *different* settings is refused rather than silently changing the
    first build's environment under it. The variables are set by the first
    build to enter and restored by the last to leave.

    Raises:
        SettingsError: Another resident build in this process is running
            under different settings.
    """
    wanted = dict(settings or {})
    with _resident_lock:
        if any(active != wanted for active in _resident_active):
            raise SettingsError(
                "Another sd.build() in this process is running under different "
                "settings; settings are environment variables, so two "
                "concurrent builds in one process cannot use different ones. "
                "Run them one after the other, or in separate processes."
            )
        if not _resident_active:
            _resident_restore.clear()
            _resident_restore.update({k: os.environ.get(k) for k in wanted})
            os.environ.update(wanted)
        _resident_active.append(wanted)
    try:
        yield
    finally:
        with _resident_lock:
            _resident_active.remove(wanted)
            if not _resident_active:
                for key, value in _resident_restore.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                _resident_restore.clear()
