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
- A **bare resume** -- ``settings`` omitted (``None``) with a build to
  resume -- reuses the settings of the build's active plan
  (:func:`resolve_settings`), as a bare re-trigger reads the stored config;
  only an explicit ``settings={}`` means "no settings" (a new plan when the
  active one had some, S14).
- Workers and ticks apply them in a scoped :func:`settings_applied` around
  the run; a resident driver applies them for the whole build with
  :func:`resident_settings`, which refuses a second concurrent build in the
  same process under different settings (the environment is per process).
  Both hold one process-wide owner token, so a resident build and a scoped
  block (a local reactive bootstrap or tick) never both install settings.
- **A process applying a build's settings serves one build at a time.**
  The deployed tick and worker functions run one input per container and
  scale by containers (refused at deploy otherwise), and
  :func:`settings_applied` / :func:`settings_owner` refuse a second build
  entering while another build's settings are installed, so a
  misconfiguration fails loudly instead of running under the wrong
  build's values.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import typing
from collections.abc import Mapping
from uuid import UUID, uuid5

from stardag.exceptions import StardagError

if typing.TYPE_CHECKING:
    from stardag.registry import RegistryABC
from stardag.utils.env import temp_env_vars

RESERVED_SETTINGS_PREFIXES = ("STARDAG_", "MODAL_")

Settings = dict[str, str]

#: The uuid5 namespace of settings hashes. Never change this value: it keys
#: every stored settings body. It is ``uuid5(<default task-id namespace>,
#: "stardag.settings_hash.v1")``, written out rather than derived from
#: ``task_uuid5_namespace_provider``, because the *registry* computes the
#: hash from the posted body and cannot see a client-side override of the
#: task namespace. Being distinct from the task-id and instance-hash
#: namespaces, a settings hash never coincides with either for the same
#: JSON.
SETTINGS_HASH_NAMESPACE = UUID("d9bc3c1c-6c3b-534d-be75-aaa4c8d71c59")

#: The settings hash of the empty settings ``{}``.
EMPTY_SETTINGS_HASH = UUID("11406eac-39d0-5b1b-9423-cfb4a1454543")


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


def settings_hash(settings: Mapping[str, str]) -> UUID:
    """The key a settings body is stored under: uuid5, like every other
    identity in the registry, over the body's canonical JSON (sorted keys,
    compact separators, non-ASCII kept, hashed as UTF-8) in
    :data:`SETTINGS_HASH_NAMESPACE`.

    The registry computes it from the posted body — a client never sends
    one — so this is the SDK's copy, for the in-memory registry and for
    tests. The empty settings ``{}`` hash to :data:`EMPTY_SETTINGS_HASH`
    (``11406eac-39d0-5b1b-9423-cfb4a1454543``).
    """
    canonical = json.dumps(
        dict(settings), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return uuid5(SETTINGS_HASH_NAMESPACE, canonical)


def _stored_needed(
    registry: "RegistryABC", build_id: "UUID | None", settings: object
) -> bool:
    from stardag.registry import is_noop_registry

    return settings is None and build_id is not None and not is_noop_registry(registry)


def resolve_settings(
    registry: "RegistryABC",
    build_id: "UUID | None",
    settings: Mapping[str, object] | None,
) -> Settings:
    """The settings a build runs under: ``settings`` validated, or -- for a
    bare resume (``settings is None`` with ``build_id``) -- the settings of
    the build's active plan, read from the registry (the frontier's
    ``settings_hash``, then ``GET /settings/{hash}``). A build with no plan
    yet resolves to ``{}``."""
    if not _stored_needed(registry, build_id, settings):
        return validate_settings(settings)
    assert build_id is not None
    settings_hash = registry.build_get_frontier(build_id).settings_hash
    if not settings_hash:
        return {}
    return validate_settings(registry.settings_get(settings_hash).body)


async def resolve_settings_aio(
    registry: "RegistryABC",
    build_id: "UUID | None",
    settings: Mapping[str, object] | None,
) -> Settings:
    """Async :func:`resolve_settings`."""
    if not _stored_needed(registry, build_id, settings):
        return validate_settings(settings)
    assert build_id is not None
    settings_hash = (await registry.build_get_frontier_aio(build_id)).settings_hash
    if not settings_hash:
        return {}
    return validate_settings((await registry.settings_get_aio(settings_hash)).body)


# One owner token for the whole process, shared by both ways of installing
# a build's settings: a scoped block (``settings_applied``/``settings_owner``
# — a tick's pass, a bootstrap, a worker's run) holds it for one build id; a
# resident ``sd.build()`` (``resident_settings``) holds it for its whole
# duration. Whichever holds it, the other kind is refused, so a resident
# build and a reactive bootstrap/tick in one process can never both install
# settings.
_owner_lock = threading.Lock()


class _ResidentOwner:
    """The owner token of the resident builds running in this process.

    Resident builds share it when their settings are equal (the variables
    are set by the first to enter and restored by the last to leave); the
    build id is not known yet when a resident build applies its settings.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.count = 0
        self.restore: dict[str, str | None] = {k: os.environ.get(k) for k in settings}

    def __str__(self) -> str:
        return "a resident sd.build()"


# The owner whose settings are installed in this process: a build id (a
# scoped block, with ``_installed_depth`` nested blocks open), a
# ``_ResidentOwner``, or ``None`` when no build owns the environment.
_installed_owner: object | None = None
_installed_depth = 0


def _describe(owner: object) -> str:
    return str(owner) if isinstance(owner, _ResidentOwner) else f"build {owner}"


@contextlib.contextmanager
def settings_owner(owner: object) -> typing.Iterator[None]:
    """Hold this process's settings for ``owner`` (a build id) for the block.

    Settings are process-global environment variables (D4), so a process
    that applies a build's settings serves one build at a time. Re-entering
    for the same owner nests; entering for a different owner while one is
    installed — another build's scoped block, or a resident ``sd.build()`` —
    is refused, rather than letting either build run under the other's
    values (or have its values removed by the other's restore).

    Raises:
        SettingsError: Another owner's settings are installed in this
            process (the message names it).
    """
    global _installed_owner, _installed_depth
    with _owner_lock:
        if _installed_owner is not None and (
            isinstance(_installed_owner, _ResidentOwner) or _installed_owner != owner
        ):
            raise SettingsError(
                f"Build {owner} cannot apply its settings: "
                f"{_describe(_installed_owner)}'s settings are already installed "
                "in this process. Settings are environment variables, so a "
                "process applying them serves one build at a time; deployed "
                "ticks and workers must run one input per container "
                "(max_concurrent_inputs=1) and scale by containers."
            )
        _installed_owner = owner
        _installed_depth += 1
    try:
        yield
    finally:
        with _owner_lock:
            _installed_depth -= 1
            if _installed_depth == 0:
                _installed_owner = None


@contextlib.contextmanager
def settings_applied(
    settings: Mapping[str, str] | None, *, owner: object
) -> typing.Iterator[None]:
    """Apply ``settings`` as environment variables for the block, restoring
    the previous values after (a tick's pass, a worker's run), on behalf of
    build ``owner``.

    Raises:
        SettingsError: Another owner's settings are installed in this
            process (see :func:`settings_owner`).
    """
    with settings_owner(owner), temp_env_vars(dict(settings or {})):
        yield


@contextlib.contextmanager
def resident_settings(settings: Mapping[str, str] | None) -> typing.Iterator[None]:
    """Apply ``settings`` for a resident build's whole duration.

    The environment is per process, so two resident builds running at once
    in one process can only share it if they agree: a second one with
    *different* settings is refused rather than silently changing the
    first build's environment under it. The variables are set by the first
    build to enter and restored by the last to leave. A resident build is
    also refused while a scoped block (a local reactive bootstrap or tick)
    holds the process's settings, and holds them against one — the same
    owner token (see :func:`settings_owner`).

    Raises:
        SettingsError: Another resident build in this process is running
            under different settings, or another build's scoped settings
            are installed (the message names it).
    """
    global _installed_owner
    wanted = dict(settings or {})
    with _owner_lock:
        installed = _installed_owner
        if installed is not None and not isinstance(installed, _ResidentOwner):
            raise SettingsError(
                f"sd.build() cannot apply its settings: {_describe(installed)}'s "
                "settings are already installed in this process. Settings are "
                "environment variables, so a process applying them serves one "
                "build at a time; run them one after the other, or in separate "
                "processes."
            )
        if installed is not None and installed.settings != wanted:
            raise SettingsError(
                "Another sd.build() in this process is running under different "
                "settings; settings are environment variables, so two "
                "concurrent builds in one process cannot use different ones. "
                "Run them one after the other, or in separate processes."
            )
        if installed is None:
            installed = _ResidentOwner(wanted)
            os.environ.update(wanted)
            _installed_owner = installed
        installed.count += 1
    try:
        yield
    finally:
        with _owner_lock:
            installed.count -= 1
            if installed.count == 0:
                for key, value in installed.restore.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                _installed_owner = None
