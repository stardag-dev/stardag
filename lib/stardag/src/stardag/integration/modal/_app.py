"""Stardag Modal integration - App and executor for running tasks on Modal.

This module provides:
- StardagApp: A wrapper around modal.App that manages builder and worker functions
- ModalTaskExecutor: A task executor that sends tasks to Modal for remote execution
- Helper functions for profile-based environment configuration

Example usage:

    import modal
    from stardag.integration.modal import StardagApp, FunctionSettings

    # Define your image (user has full control)
    image = (
        modal.Image.debian_slim()
        .pip_install("pandas", "numpy", "stardag")
        .add_local_python_source("my_code")  # Local sources last for caching
    )

    # Create the app (functions are NOT created yet)
    stardag_app = StardagApp(
        "my-app",
        builder_settings=FunctionSettings(image=image),
        worker_settings={"default": FunctionSettings(image=image)},
    )

    # Deploy with: stardag modal deploy my_app.py --profile prod
    # The --profile flag injects environment variables as a Modal secret

    # To run tasks remotely (after deployment):
    from my_tasks import my_task
    stardag_app.build_spawn(my_task)  # Looks up deployed function by name
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import pathlib
import traceback as tb_module
import typing
from uuid import UUID

import modal

from stardag import BaseTask, TaskStruct, build, flatten_task_struct
from stardag._core.base_task import _has_custom_run, _has_custom_run_aio
from stardag.exceptions import StardagError
from stardag.build import (
    BuildExitStatus,
    BuildTaskStore,
    FailMode,
    DetachedExecutionStatus,
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
    TickConfig,
    discover_and_register_aio,
    get_current_build_id,
    run_tick_aio,
)
from stardag.build._base import GlobalLockConfig
from stardag.build._base import BuildSummary
from stardag.build._reactive import claim_ttl_seconds
from stardag.build._task_modules import (
    PickleElisionPlan,
    TaskModulesError,
    declared_task_module_patterns,
    expand_task_module_patterns,
    format_uncovered_message,
    import_task_modules,
    plan_pickle_elision,
    set_declared_task_module_patterns,
    uncovered_task_classes,
    validate_task_module_patterns,
)
from stardag.config import clear_config_cache, config_provider, load_config
from stardag.integration.modal._target import (
    MODAL_VOLUME_URI_PREFIX,
    get_default_volume_mount_path,
    get_volume_name_and_path,
)
from stardag.registry._base import (
    NoOpRegistry,
    get_git_commit_hash,
    registry_provider,
)
from stardag.registry._lock import RegistryGlobalConcurrencyLockManager
from stardag.utils.env import temp_env_vars

try:
    from stardag.integration.prefect import (
        build_flow as prefect_build_flow,
    )
    from stardag.integration.prefect import (
        create_markdown,
        upload_task_on_complete_artifacts,
    )
except ImportError:
    prefect_build_flow = None
    create_markdown = None
    upload_task_on_complete_artifacts = None

logger = logging.getLogger(__name__)


# --- Configuration helpers ---


def get_profile_env_vars(profile: str | None = None) -> dict[str, str]:
    """Get environment variables from a stardag profile for Modal deployment.

    These environment variables configure the stardag SDK inside Modal containers
    to connect to the correct registry, workspace, and environment.

    Args:
        profile: Profile name to use. If None, uses the active profile
            (from STARDAG_PROFILE env var or default profile in config).

    Returns:
        Dict of environment variables to inject into Modal functions:
        - STARDAG_API_URL: API endpoint
        - STARDAG_WORKSPACE_ID: Workspace UUID
        - STARDAG_ENVIRONMENT_ID: Environment UUID
        - STARDAG_TARGET_ROOTS: JSON dict of target roots (pydantic-settings parses this)
        - COMMIT_HASH: Current git commit (for traceability)

    Example:
        >>> env_vars = get_profile_env_vars("production")
        >>> print(env_vars)
        {
            'STARDAG_API_URL': 'https://api.stardag.com',
            'STARDAG_WORKSPACE_ID': '...',
            'STARDAG_ENVIRONMENT_ID': '...',
            'STARDAG_TARGET_ROOTS': '{"default": "s3://bucket/prefix"}',
            'COMMIT_HASH': 'abc123...'
        }
    """
    # Load config for specific profile if provided
    if profile:
        # Temporarily set STARDAG_PROFILE to load that profile's config
        old_profile = os.environ.get("STARDAG_PROFILE")
        os.environ["STARDAG_PROFILE"] = profile
        try:
            clear_config_cache()
            config = load_config()
        finally:
            if old_profile is not None:
                os.environ["STARDAG_PROFILE"] = old_profile
            else:
                os.environ.pop("STARDAG_PROFILE", None)
            clear_config_cache()
    else:
        config = config_provider.get()

    env_vars: dict[str, str] = {}

    reg = config.registry
    if reg:
        env_vars["STARDAG_API_URL"] = reg.url
        if reg.workspace_id:
            env_vars["STARDAG_WORKSPACE_ID"] = reg.workspace_id
        if reg.environment_id:
            env_vars["STARDAG_ENVIRONMENT_ID"] = reg.environment_id

    # Add target roots as JSON (pydantic-settings parses JSON for nested fields)
    if config.target.roots:
        env_vars["STARDAG_TARGET_ROOTS"] = json.dumps(config.target.roots)

    # Add git commit for traceability
    commit_hash = get_git_commit_hash()
    if commit_hash:
        env_vars["COMMIT_HASH"] = commit_hash

    return env_vars


def get_profile_secret(profile: str | None = None) -> modal.Secret:
    """Create a Modal secret from a stardag profile's environment variables.

    This is the recommended way to inject profile configuration into Modal
    functions at runtime, rather than baking them into the image.

    Args:
        profile: Profile name to use. If None, uses the active profile.

    Returns:
        A modal.Secret that can be passed to FunctionSettings.secrets.

    Example:
        >>> secret = get_profile_secret("production")
        >>> stardag_app = StardagApp(
        ...     "my-app",
        ...     builder_settings=FunctionSettings(
        ...         image=my_image,
        ...         secrets=[secret],  # Injected at runtime
        ...     ),
        ...     ...
        ... )
    """
    env_vars = get_profile_env_vars(profile)
    return modal.Secret.from_dict(typing.cast(dict[str, str | None], env_vars))


class TargetRootsVolumes(typing.NamedTuple):
    """Result of get_target_roots_volumes().

    Attributes:
        by_root_key: Dict of target root key to Modal Volume instance.
        by_volume_name: Dict of volume name to Modal Volume instance (deduped).
    """

    by_root_key: dict[str, modal.Volume]
    by_volume_name: dict[str, modal.Volume]


def get_target_roots_volumes(
    target_roots: dict[str, str] | None = None,
    create_if_missing: bool = True,
) -> TargetRootsVolumes:
    """Get Modal volumes for configured target roots.

    Scans target roots for ``modalvol://`` URIs and returns the corresponding
    Modal Volume objects, both keyed by target root name and deduped by
    volume name.

    Args:
        target_roots: Dict of target root key to URI or None (default from config).
        create_if_missing: Whether to create the Modal volume if it doesn't exist.
            When True, volumes are eagerly hydrated to trigger creation.
            When False, volumes are lazy references (hydrated by Modal at deploy time).

    Returns:
        TargetRootsVolumes with volumes keyed by root key and by volume name.
    """
    if target_roots is None:
        config = config_provider.get()
        target_roots = config.target.roots

    by_root_key: dict[str, modal.Volume] = {}
    by_volume_name: dict[str, modal.Volume] = {}
    for key, uri in target_roots.items():
        if not uri.startswith(MODAL_VOLUME_URI_PREFIX):
            continue
        volume_name, _ = get_volume_name_and_path(uri)
        if volume_name not in by_volume_name:
            vol = modal.Volume.from_name(
                volume_name, create_if_missing=create_if_missing
            )
            if create_if_missing:
                vol.hydrate()
            by_volume_name[volume_name] = vol
        by_root_key[key] = by_volume_name[volume_name]

    return TargetRootsVolumes(by_root_key=by_root_key, by_volume_name=by_volume_name)


class FinalizeResult(typing.NamedTuple):
    """Result of StardagApp.finalize().

    Attributes:
        volumes: Dict of target root key to Modal Volume instance.
        functions: List of created Modal function names.
        volume_mounts: Dict of mount_path -> volume_name for auto-mounted volumes.
        auto_volumes: Dict of mount_path -> Volume for auto-mounted volumes.
        task_modules: The app's ``task_modules`` patterns expanded to the
            concrete, sorted module list baked into the deployed scheduler
            tick (empty when the app opted out). Surfaced so the CLI can
            report what the deployment will import.
    """

    volumes: dict[str, modal.Volume]
    functions: list[str]
    volume_mounts: dict[str, str] = {}
    auto_volumes: dict[str, modal.Volume] = {}
    task_modules: list[str] = []


# --- Function settings ---


def _dedupe_secrets(secrets: list[modal.Secret]) -> list[modal.Secret]:
    """De-duplicate Modal secrets by name, preserving order.

    Named secrets (``Secret.from_name``) dedupe by name so a secret
    propagated from the builder to a worker that also declares it is applied
    once. Secrets without a usable name (e.g. ``Secret.from_dict``) fall back
    to object identity.
    """
    seen: set[str | int] = set()
    result: list[modal.Secret] = []
    for secret in secrets:
        key: str | int = getattr(secret, "name", None) or id(secret)
        if key in seen:
            continue
        seen.add(key)
        result.append(secret)
    return result


def _run_watchdog_sweep(
    registry: typing.Any,
    tick: typing.Callable[..., typing.Any],
    sweep_limit: int = 100,
    reactive_app_name: str | None = None,
) -> None:
    """One watchdog pass: tick every running build, without lingering.

    ``linger_seconds=0`` (one frontier pass per build) is essential: the
    sweep runs ticks sequentially in one function call — persisted linger
    settings (default 120 s) would blow through the function timeout after
    a couple of builds and starve the rest of the safety-net tick.

    ``reactive_app_name`` scopes the listing to the builds this app owns.
    Without it, ``sweep_limit`` is spent on whatever RUNNING builds happen
    to be most recently active in the environment — including builds no
    tick can ever advance (resident-orchestrator builds, and builds whose
    orchestrator died without emitting a terminal event, which stay RUNNING
    forever). Once those exceed the limit the safety net stops reaching
    genuine reactive builds entirely, and silently.

    Trade-off: scoping drops the incidental cross-app coverage a sweep used
    to provide, where app A's watchdog would tick app B's builds and the
    tick would forward the wake-up to B. That coverage was accidental and
    unreliable (it competed for the same limit); the owner app's own
    watchdog is the supported mechanism, and forwarding still handles
    wake-ups from workers of a previous owner. The one case that regresses
    is a build owned by an app deployed WITHOUT a watchdog, which
    build_trigger already warns about at trigger time.
    """
    if type(registry) is NoOpRegistry:
        logger.warning("Tick watchdog: no registry configured; nothing to do.")
        return
    # Scoping is server-side now that `build_list_running` is expressed in
    # terms of `build_list`, so every RegistryABC gets it and the signature
    # shim this used to need went with it. `scoped` still exists because
    # the truncation remedy below differs by it.
    scoped = reactive_app_name is not None
    running_builds = registry.build_list_running(
        limit=sweep_limit, reactive_app_name=reactive_app_name
    )
    scope = (
        f"reactive builds owned by {reactive_app_name!r}"
        if scoped
        else "running builds"
    )
    if len(running_builds) >= sweep_limit:
        # The remedy follows `scoped`, not whether a name was *asked* for:
        # a scoping request the registry cannot honour still yields a
        # listing capped by RUNNING builds of every kind, and "run fewer
        # reactive builds per app" would then be advice about the wrong
        # population.
        remedy = (
            "Cancel or clean up builds that are RUNNING but abandoned, or "
            "reduce the number of concurrent reactive builds for this app."
            if scoped
            else "This listing was not scoped to a reactive app, so the cap "
            "was consumed by RUNNING builds of every kind — most likely "
            "abandoned ones. Clean those up (`stardag builds cleanup`); an "
            "upgraded registry would also scope this listing."
        )
        logger.warning(
            f"Tick watchdog: {sweep_limit}+ {scope}; only the {sweep_limit} "
            "most recently active are swept, so a less-recently-active build "
            f"may not be ticked this period. {remedy}"
        )
    # Each tick re-reads the build's reactive metadata (GET /builds/{id}) to
    # resolve the owner app and the stored tick_kwargs, so that probe is not
    # redundant with the server-side filter — but it is now paid only for
    # builds that survived the filter, instead of once per RUNNING build in
    # the environment. It also stays the correctness backstop: a tick on a
    # non-reactive build (which a degraded, unfiltered listing can still
    # yield) no-ops on that gate.
    for running_build_id in running_builds:
        try:
            tick(str(running_build_id), tick_kwargs={"linger_seconds": 0})
        except Exception:
            logger.exception(f"Watchdog tick failed for build {running_build_id}")


def _fail_build_on_trigger_error(
    registry: typing.Any,
    build_id: UUID,
    stage: str,
    error: BaseException,
) -> None:
    """Emit a terminal BUILD_FAILED for a trigger that died mid-way.

    A build's status is derived from its events: once ``build_start`` has
    happened, only a terminal event can move the build out of RUNNING. If
    the trigger raises before it is finished, nobody else will ever emit
    one — no orchestrator was ever spawned — so the trigger must do it.

    Best-effort by construction: the caller re-raises the original error
    unconditionally, and a failure to record the terminal event is logged
    rather than raised, so a secondary registry error can never mask the
    root cause the user needs to see.
    """
    try:
        registry.build_fail(
            build_id,
            f"Reactive trigger failed during {stage}: {type(error).__name__}: {error}",
        )
        logger.info(
            f"Reactive trigger failed during {stage}; marked build "
            f"{build_id} as failed."
        )
    except Exception:
        logger.exception(
            f"Reactive trigger failed during {stage}, and marking build "
            f"{build_id} as failed ALSO failed; the build may be left in "
            "RUNNING status and should be cancelled manually."
        )


_TICK_KWARGS_ALLOWED = ("linger_seconds", "poll_interval_seconds", "fail_mode")


def _build_tick_config(
    stored_tick_kwargs: dict[str, typing.Any] | None,
    tick_kwargs: dict[str, typing.Any] | None,
    limit_key_selector: "typing.Callable[[BaseTask], typing.Sequence[str]] | None",
) -> TickConfig:
    """Assemble a TickConfig for one tick invocation.

    Precedence: explicit ``tick_kwargs`` (manual/ops invocations) over the
    build's stored ``reactive_tick_kwargs`` (set at trigger time in the
    registry — shared by all ticks) over TickConfig defaults. The
    concurrency-limit key selector is deployed-app configuration (callables
    can't ride in the JSON tick config).
    """
    config_kwargs: dict[str, typing.Any] = {
        **(stored_tick_kwargs or {}),
        **(tick_kwargs or {}),
    }
    if "fail_mode" in config_kwargs:
        config_kwargs["fail_mode"] = FailMode(config_kwargs["fail_mode"])
    return TickConfig(limit_key_selector=limit_key_selector, **config_kwargs)


def _validate_tick_kwargs(
    tick_kwargs: dict[str, typing.Any] | None,
) -> dict[str, typing.Any] | None:
    """Validate + JSON-normalize reactive tick_kwargs.

    They are persisted in the build's ``reactive_tick_kwargs`` in the
    registry (JSON) so all ticks of the build share them — hence only
    JSON-scalar TickConfig fields are allowed here. ``fail_mode`` may be
    passed as a FailMode and is stored as its string value.
    """
    if not tick_kwargs:
        return tick_kwargs
    unknown = set(tick_kwargs) - set(_TICK_KWARGS_ALLOWED)
    if unknown:
        raise TypeError(
            f"Unsupported tick_kwargs {sorted(unknown)}; allowed (JSON-"
            f"persistable TickConfig fields): {list(_TICK_KWARGS_ALLOWED)}. "
            "Callables like a concurrency-limit key selector belong in the "
            "deployed app configuration, not per-trigger kwargs."
        )
    normalized = dict(tick_kwargs)
    if "fail_mode" in normalized:
        normalized["fail_mode"] = str(FailMode(normalized["fail_mode"]))
    return normalized


class BuildTriggerResult(typing.NamedTuple):
    """Result of :meth:`StardagApp.build_trigger`.

    Attributes:
        build_id: The registry build id minted (or reused) at the trigger
            point. Pass it back to ``build_trigger(..., build_id=...)`` to
            re-attach/resume the same build.
        function_call: The Modal ``FunctionCall`` handle for the spawned
            build function invocation. Call ``.get()`` to block on the
            result if needed.
    """

    build_id: UUID
    function_call: typing.Any


class FunctionSettings(typing.TypedDict, total=False):
    """Settings for Modal function configuration.

    These settings are passed to modal.App.function() when creating
    builder and worker functions.

    Attributes:
        image: Required. The Modal image to use for the function.
        gpu: GPU configuration (e.g., "A10G", "T4", or list for fallback).
        cpu: CPU cores (float or (min, max) tuple).
        memory: Memory in MB (int or (min, max) tuple).
        timeout: Function timeout in seconds.
        volumes: Dict of mount path to Volume or CloudBucketMount.
        secrets: List of Modal secrets to inject.
        concurrency_limit: Max number of concurrent containers.
        allow_concurrent_inputs: Max concurrent inputs per container.
        container_idle_timeout: Seconds before idle container shuts down.
        keep_warm: Number of containers to keep warm.
        ephemeral_disk: Ephemeral disk size in MB.
        retries: Number of retries on failure.
    """

    image: typing.Required[modal.Image]
    gpu: str | list[str]
    cpu: float | tuple[float, float]
    memory: int | tuple[int, int]
    timeout: int
    volumes: dict[
        typing.Union[str, pathlib.PurePosixPath],
        typing.Union[modal.Volume, modal.CloudBucketMount],
    ]
    secrets: list[modal.Secret]
    concurrency_limit: int
    allow_concurrent_inputs: int
    container_idle_timeout: int
    keep_warm: int
    ephemeral_disk: int
    retries: int


# --- Worker selector ---


WorkerSelection = typing.Union[str, tuple[str, dict[str, str]]]
"""Return type of a :data:`WorkerSelector`.

Either a worker name (``str``), or a ``(worker_name, env_overrides)`` tuple
where ``env_overrides`` is a dict of environment variables to set temporarily
around the task's ``run`` call inside the worker container (e.g. to tune
task-specific execution knobs such as worker/thread counts or batch sizes).
See :meth:`Runner.__call__`.
"""

WorkerSelector = typing.Callable[[BaseTask], WorkerSelection]
"""Type for functions that select which worker to use for a task.

A selector returns either a worker name, or a ``(worker_name, env_overrides)``
tuple (see :data:`WorkerSelection`).
"""


def _normalize_worker_selection(
    selection: WorkerSelection,
) -> tuple[str, dict[str, str] | None]:
    """Split a :data:`WorkerSelection` into ``(worker_name, env_overrides)``.

    Accepts either a bare worker name or a ``(worker_name, env_overrides)``
    tuple and always returns the two-tuple form (``env_overrides`` is ``None``
    when the selector returned a bare name).
    """
    if isinstance(selection, tuple):
        worker_name, env_overrides = selection
        return worker_name, env_overrides
    return selection, None


def _default_worker_selector(task: BaseTask) -> WorkerSelection:
    """Default worker selector - always returns 'default'."""
    return "default"


def _callable_accepts_env_overrides(fn: typing.Callable[..., typing.Any]) -> bool:
    """Whether ``fn`` accepts an ``env_overrides`` argument.

    Used to stay backward-compatible with custom ``RunFunction`` implementations
    written against the older ``(task)``-only signature (before the optional
    ``env_overrides`` parameter was added).
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "env_overrides":
            return True
    return False


# --- Task executor ---


def _is_transient_modal_error(exception: BaseException) -> bool:
    """Best-effort classification of transport-level (retryable) errors.

    Anything raised by ``FunctionCall.get`` that reflects the CALL's outcome
    (``modal.exception.RemoteError``, user exceptions re-raised from the
    function, expired results) is terminal; connection/socket/gRPC-transport
    failures are not — they say nothing about the call.
    """
    if isinstance(exception, (OSError, ConnectionError)):
        return True
    module = type(exception).__module__ or ""
    if module.startswith("grpclib") or module.startswith("h2"):
        return True
    return type(exception).__name__ in ("ClientClosed", "StreamTerminatedError")


MODAL_EXECUTOR_NAME = "modal"
"""Executor name recorded with detached executions (see DetachedHandle)."""

STARDAG_BUILD_ID_ENV = "STARDAG_BUILD_ID"
"""Env var through which the build id reaches Modal workers.

Injected into ``env_overrides`` by :class:`ModalTaskExecutor` (so it is also
set as a process env var around the task's run) and read by
:class:`Runner` to report the task's lifecycle events from inside the
worker. Riding on ``env_overrides`` keeps the worker function signature
unchanged — older deployed workers simply apply it as a harmless env var.
"""

STARDAG_MODAL_APP_NAME_ENV = "STARDAG_MODAL_APP_NAME"
"""Env var carrying the Modal app name to workers (reactive scheduling).

Lets a worker wake the scheduler by spawning the app's ``tick`` function
when it finishes a task. Transported like ``STARDAG_BUILD_ID``.
"""

STARDAG_REACTIVE_ENV = "STARDAG_REACTIVE"
"""Env var flagging reactive scheduling to workers ("1" when reactive).

In reactive mode the worker additionally registers dynamically yielded
deps (with task-store persistence) and wakes the scheduler after terminal
events — there is no resident orchestrator to do either.
"""

STARDAG_CLAIM_TTL_SECONDS_ENV = "STARDAG_CLAIM_TTL_SECONDS"
"""Env var carrying the claim TTL the orchestrator derived for this task.

The worker's own TASK_STARTED is a start like any other, so without this
it would re-stamp the claim with the registry's generic default and undo
the orchestrator's derivation (see
``stardag.build._reactive.claim_ttl_seconds``). Forwarding it also *improves*
the bound: the worker's start is recorded when execution actually begins, so
the expiry is re-based off the real start rather than off the pre-spawn
claim, which absorbed however long the call sat queued.
"""

STARDAG_MODAL_WORKSPACE_ENV = "STARDAG_MODAL_WORKSPACE"
"""Env var carrying the resolved Modal workspace name to workers.

Part of the executor-metadata channel: the orchestrator resolves the
workspace once (token lookup or explicit override) and forwards it so the
worker's self-reported TASK_STARTED carries the same metadata dict.
Transported like ``STARDAG_BUILD_ID``.
"""

STARDAG_MODAL_ENVIRONMENT_ENV = "STARDAG_MODAL_ENVIRONMENT"
"""Env var carrying the Modal environment name to workers (see
``STARDAG_MODAL_WORKSPACE``)."""

STARDAG_MODAL_FUNCTION_NAME_ENV = "STARDAG_MODAL_FUNCTION_NAME"
"""Env var carrying the Modal function name (``worker_<name>``) to workers
(see ``STARDAG_MODAL_WORKSPACE``)."""

STARDAG_MODAL_APP_ID_ENV = "STARDAG_MODAL_APP_ID"
"""Env var carrying the resolved Modal app id (``ap-…``) to workers.

Part of the executor-metadata channel: the orchestrator resolves the app
id once (best-effort ``modal.App.lookup``) and forwards it so the worker's
self-reported start carries it too. Lets the UI build stable, stop/
redeploy-proof dashboard deep links (the app-id URL form outlives a given
deployed app version). Transported like ``STARDAG_BUILD_ID``."""

STARDAG_MODAL_FUNCTION_ID_ENV = "STARDAG_MODAL_FUNCTION_ID"
"""Env var carrying the Modal function id (``fu-…``) to workers (see
``STARDAG_MODAL_APP_ID``)."""


# Cache for the token-derived Modal workspace name. Resolved at most once
# per process (including failed lookups — metadata is best-effort and a
# broken token shouldn't re-pay the lookup timeout on every task).
_MODAL_WORKSPACE_UNRESOLVED = object()
_modal_workspace_cache: typing.Any = _MODAL_WORKSPACE_UNRESOLVED
# Serialises cold-start lookups so a burst of concurrent starts performs
# one network lookup instead of N parallel ones. Safe to share across
# sequential event loops: it is never held across loop boundaries.
_modal_workspace_lock = asyncio.Lock()


async def _get_modal_workspace_aio() -> str | None:
    """Best-effort Modal workspace name for the configured token (cached)."""
    global _modal_workspace_cache
    if _modal_workspace_cache is not _MODAL_WORKSPACE_UNRESOLVED:
        return typing.cast("str | None", _modal_workspace_cache)
    async with _modal_workspace_lock:
        if _modal_workspace_cache is _MODAL_WORKSPACE_UNRESOLVED:
            # Prefer the workspace baked into the container env at deploy
            # time. The token lookup below only works where a Modal token is
            # configured — the local triggering/deploy process — NOT inside a
            # Modal container (worker/tick/build), which is exactly where
            # task-level executor metadata is produced. finalize() resolves
            # the workspace locally and injects it as STARDAG_MODAL_WORKSPACE
            # so containers read it here instead of failing the token lookup.
            env_workspace = os.environ.get(STARDAG_MODAL_WORKSPACE_ENV)
            if env_workspace:
                _modal_workspace_cache = env_workspace
            else:
                try:
                    _modal_workspace_cache = await _lookup_modal_workspace_aio()
                except Exception as e:
                    # Cache the failure too: metadata is best-effort and a
                    # broken token / unreachable Modal API must neither raise
                    # into a task start nor re-pay the lookup on every start.
                    _modal_workspace_cache = None
                    logger.debug(
                        f"Modal workspace lookup failed (metadata omitted): {e}"
                    )
    return typing.cast("str | None", _modal_workspace_cache)


async def _lookup_modal_workspace_aio() -> str | None:
    from modal.config import _lookup_workspace
    from modal.config import config as modal_config

    server_url = modal_config.get("server_url")
    token_id = modal_config.get("token_id")
    token_secret = modal_config.get("token_secret")
    if not (server_url and token_id and token_secret):
        return None
    response = await _lookup_workspace(server_url, token_id, token_secret)
    # `workspace_name` is the org/display name and is empty for personal
    # workspaces; `username` is the account slug used in dashboard URLs
    # (what `modal token info` prints as "Workspace"). Prefer the explicit
    # workspace name when present, else fall back to the username — else the
    # (common) personal-workspace case resolves to nothing.
    return response.workspace_name or response.username or None


def _get_modal_workspace() -> str | None:
    """Sync wrapper for :func:`_get_modal_workspace_aio` (cached).

    Returns None (without caching a failure) when called from inside a
    running event loop where ``asyncio.run`` is unavailable.
    """
    global _modal_workspace_cache
    if _modal_workspace_cache is not _MODAL_WORKSPACE_UNRESOLVED:
        return typing.cast("str | None", _modal_workspace_cache)
    # The deploy-baked env works regardless of an event loop (no lookup).
    env_workspace = os.environ.get(STARDAG_MODAL_WORKSPACE_ENV)
    if env_workspace:
        _modal_workspace_cache = env_workspace
        return env_workspace
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_get_modal_workspace_aio())
    return None


def _get_modal_environment() -> str | None:
    """The active Modal environment name from Modal config, if any."""
    from modal.config import config as modal_config

    return modal_config.get("environment") or None


# Upper bound (seconds) on the best-effort Modal id lookups performed on the
# critical path before ``spawn``. Matches the 3 s deadline Modal's own
# ``_lookup_workspace`` gRPC call uses: a *hung* (not merely refused) Modal
# API must not stall the first task start beyond this cap. On timeout the id
# is treated as an ordinary best-effort failure (key omitted, debug-logged).
_MODAL_ID_LOOKUP_TIMEOUT_SECONDS = 3.0


async def _get_modal_app_id_aio(
    app_name: str, environment_name: str | None
) -> str | None:
    """Best-effort Modal app id (``ap-…``) for the deployed app.

    Unlike the token workspace lookup, ``modal.App.lookup`` resolves both
    locally and *inside a Modal container* (worker/tick/build) — which is
    where task-level executor metadata is produced — so it needs no
    deploy-baked env fallback. Never raises: on any failure (including the
    bounded-timeout expiry) the id is omitted from the executor metadata and
    the failure logged at debug. Bounded by ``_MODAL_ID_LOOKUP_TIMEOUT_SECONDS``
    so a hung Modal API cannot stall a task start. Resolution is cached by the
    once-resolved base executor metadata dict.

    Caveat: with ``environment_name=None`` the lookup resolves against the
    *config-default* Modal environment. If the app is deployed to one
    environment but local config defaults to another that happens to hold a
    same-named app, the id (like the ``environment`` metadata key in that same
    scenario) will be that of the wrong app. Pass the resolved environment to
    avoid this.
    """
    try:
        app = await asyncio.wait_for(
            modal.App.lookup.aio(app_name, environment_name=environment_name),
            timeout=_MODAL_ID_LOOKUP_TIMEOUT_SECONDS,
        )
        return app.app_id
    except Exception as e:
        logger.debug(f"Modal app id lookup failed (metadata omitted): {e}")
        return None


async def _get_modal_function_id_aio(function: modal.Function) -> str | None:
    """Best-effort Modal function id (``fu-…``) for a worker function.

    Hydrates the (lazy) ``modal.Function`` handle if needed and reads
    ``object_id``. ``hydrate`` is a no-op when already hydrated. Never
    raises: on failure (including the bounded-timeout expiry) the id is
    omitted and the failure logged at debug. Bounded by
    ``_MODAL_ID_LOOKUP_TIMEOUT_SECONDS`` so a hung Modal API cannot stall a
    task start.
    """
    try:
        await asyncio.wait_for(
            function.hydrate.aio(), timeout=_MODAL_ID_LOOKUP_TIMEOUT_SECONDS
        )
        return function.object_id
    except Exception as e:
        logger.debug(f"Modal function id resolution failed (metadata omitted): {e}")
        return None


class ModalTaskExecutor(TaskExecutorABC):
    """Task executor that sends tasks to Modal for remote execution.

    This executor submits tasks to Modal worker functions. Use with
    RoutedTaskExecutor to route some tasks to Modal and others locally.

    By default tasks are executed *detached* (``worker.spawn`` + a tracked
    ``FunctionCall``): the worker invocation survives this process, its
    function call id is recorded in the registry, and a resumed build
    re-attaches to still-running workers instead of re-executing them.
    Pass ``detached=False`` for the legacy blocking ``worker.remote`` mode.

    Example:
        from stardag.build import HybridConcurrentTaskExecutor, RoutedTaskExecutor

        modal_executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda task: "gpu" if needs_gpu(task) else "default",
        )
        local_executor = HybridConcurrentTaskExecutor()

        routed = RoutedTaskExecutor(
            executors={"modal": modal_executor, "local": local_executor},
            router=lambda task: "modal" if run_on_modal(task) else "local",
        )
        build([task], task_executor=routed)
    """

    def __init__(
        self,
        *,
        modal_app_name: str,
        worker_selector: WorkerSelector,
        detached: bool = True,
        worker_reports_lifecycle: bool = True,
        reactive: bool = False,
        modal_workspace: str | None = None,
        worker_timeouts: dict[str, int] | None = None,
    ):
        """Initialize Modal executor.

        Args:
            modal_app_name: Name of the Modal app with worker functions.
            worker_selector: Function that selects which Modal worker to use per task.
            detached: Execute tasks as detached spawned function calls
                (restart-safe, re-attachable, explicitly cancellable).
                False restores the legacy blocking ``remote`` calls.
            worker_reports_lifecycle: Whether the deployed workers report the
                task lifecycle (started/completed/suspended/failed events +
                artifacts) themselves via the default :class:`Runner`. When
                True the build engine skips its own completed/suspended/
                resumed reporting for Modal-routed tasks. Set False when
                driving an app deployed with an older stardag version whose
                workers don't self-report, or when using a custom
                ``run_function`` without lifecycle reporting.
            modal_workspace: Explicit Modal workspace name for the executor
                metadata recorded with task starts (UI deep links). Default:
                resolved once from the configured Modal token, best-effort.
            worker_timeouts: Per-worker Modal function ``timeout`` (seconds),
                as declared in the app's ``worker_settings``. Only the
                deploy process can see those settings, so they are passed in
                rather than looked up. Used to derive the execution-claim
                TTL recorded with every start (see
                :meth:`execution_timeout_seconds`); an absent worker simply
                yields no timeout and the registry's default applies.
        """
        self.modal_app_name = modal_app_name
        self.worker_selector = worker_selector
        self.detached = detached
        self.worker_timeouts = dict(worker_timeouts or {})
        self.worker_reports_lifecycle = worker_reports_lifecycle
        # Reactive scheduling: forward the app name + reactive flag so
        # workers register their dynamic deps and wake the scheduler tick.
        self.reactive = reactive
        self.modal_workspace = modal_workspace
        # Executor metadata shared by every start this executor records
        # (per-task starts add the worker function name). Resolved lazily
        # at the first invocation, best-effort — metadata must never fail
        # or delay a task start beyond the one cached lookup.
        self._base_executor_metadata: dict[str, typing.Any] | None = None
        # Cache of worker name -> modal.Function. ``modal.Function.from_name``
        # returns a lazy handle (no network call until invoked), but it is
        # invoked on every ``submit`` so we memoize it per worker name to avoid
        # recreating the handle for every task.
        self._worker_functions: dict[str, modal.Function] = {}
        # Cache of worker name -> resolved function id (``fu-…``), best-effort.
        # A ``None`` value is a *resolved* negative (a failed/timed-out
        # hydration) and is kept so a persistently failing lookup is not
        # re-paid on every task start — membership, not truthiness, marks
        # "resolved". Mirrors the once-resolved memoization of the base
        # metadata dict (which likewise caches a missing app id).
        self._worker_function_ids: dict[str, str | None] = {}
        # One-time (per executor) skew-visibility log; see reports_lifecycle.
        self._reports_lifecycle_logged = False
        # In-flight detached executions by task UUID, for explicit cancel().
        # Asyncio cancellation of ``FunctionCall.get`` does NOT stop the
        # remote call (unlike ``remote.aio``), so FAIL_FAST relies on this.
        self._in_flight: dict[UUID, modal.FunctionCall] = {}

    def _get_worker_function(self, worker_name: str) -> modal.Function:
        """Return the (memoized) ``modal.Function`` handle for a worker."""
        worker_function = self._worker_functions.get(worker_name)
        if worker_function is None:
            worker_function = modal.Function.from_name(
                app_name=self.modal_app_name,
                name=f"worker_{worker_name}",
            )
            self._worker_functions[worker_name] = worker_function
        return worker_function

    async def _get_base_executor_metadata(self) -> dict[str, typing.Any] | None:
        """Resolve the executor metadata shared by all starts (cached).

        Best-effort: resolution failures are logged at debug level and
        yield the identity-only dict — never an exception.
        """
        if self._base_executor_metadata is None:
            metadata: dict[str, typing.Any] = {
                "kind": MODAL_EXECUTOR_NAME,
                "app_name": self.modal_app_name,
            }
            environment: str | None = None
            try:
                workspace = self.modal_workspace or await _get_modal_workspace_aio()
                if workspace:
                    metadata["workspace"] = workspace
                environment = _get_modal_environment()
                if environment:
                    metadata["environment"] = environment
            except Exception:
                logger.debug(
                    "Failed to resolve Modal workspace/environment for "
                    "executor metadata",
                    exc_info=True,
                )
            # App id (``ap-…``): app-wide, so it lives in the base metadata
            # alongside workspace/environment. Resolved once and cached here
            # (the base metadata is memoized per executor). Best-effort —
            # _get_modal_app_id_aio never raises.
            app_id = await _get_modal_app_id_aio(self.modal_app_name, environment)
            if app_id:
                metadata["app_id"] = app_id
            self._base_executor_metadata = metadata
        return self._base_executor_metadata

    async def _metadata_for_worker(
        self, worker_name: str
    ) -> dict[str, typing.Any] | None:
        """Base executor metadata + the worker's function name/id (best-effort)."""
        try:
            base_metadata = await self._get_base_executor_metadata()
            if base_metadata is None:
                return None
            metadata = {**base_metadata, "function_name": f"worker_{worker_name}"}
            # Function id (``fu-…``): per-worker, so it lives here alongside
            # the function name. Best-effort — hydrate the worker handle and
            # read object_id; _get_modal_function_id_aio never raises. Cached
            # per worker name (success *and* failure): a resolved ``None`` is
            # kept so a broken/hung hydration is not re-attempted on every
            # start (membership marks "resolved", not truthiness).
            if worker_name not in self._worker_function_ids:
                self._worker_function_ids[
                    worker_name
                ] = await _get_modal_function_id_aio(
                    self._get_worker_function(worker_name)
                )
            function_id = self._worker_function_ids[worker_name]
            if function_id:
                metadata["function_id"] = function_id
            return metadata
        except Exception:
            logger.debug("Failed to resolve Modal executor metadata", exc_info=True)
            return None

    async def get_executor_metadata(
        self, task: BaseTask
    ) -> dict[str, typing.Any] | None:
        """Executor metadata for ``task`` without spawning anything.

        Runs the worker selector (idempotent) to resolve the function
        name — lets slot-acquiring TASK_STARTED events recorded before
        the spawn carry the same metadata as the post-spawn start.
        """
        try:
            worker_name, _ = _normalize_worker_selection(self.worker_selector(task))
        except Exception:
            logger.debug(
                "Worker selection failed while resolving executor metadata",
                exc_info=True,
            )
            return None
        return await self._metadata_for_worker(worker_name)

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        """The Modal ``timeout`` of the worker function this task routes to.

        Modal kills a function call at its ``timeout``, so this is a hard
        upper bound on how long an execution of ``task`` can be alive — the
        one fact that makes an execution claim's expiry defensible rather
        than a guess.

        Returns None when the deployed settings were not passed in (see
        ``worker_timeouts``), when the selected worker declares no timeout,
        or when worker selection fails: none of those is a reason to fail a
        start, and the registry's own default covers them.
        """
        if not self.worker_timeouts:
            return None
        try:
            worker_name, _ = _normalize_worker_selection(self.worker_selector(task))
        except Exception:
            logger.debug(
                "Worker selection failed while resolving the execution timeout",
                exc_info=True,
            )
            return None
        timeout = self.worker_timeouts.get(worker_name)
        return float(timeout) if timeout is not None else None

    async def _prepare_invocation(
        self, task: BaseTask
    ) -> tuple[modal.Function, dict[str, str] | None, dict[str, typing.Any] | None]:
        """Resolve the worker function, env overrides, and executor metadata.

        When ``worker_reports_lifecycle`` and an enclosing build is active,
        the build id is injected as the ``STARDAG_BUILD_ID`` env override so
        the worker-side :class:`Runner` can report lifecycle events. The
        resolved executor metadata rides along the same channel
        (``STARDAG_MODAL_*``) so worker self-reported starts carry it too —
        as does the derived claim TTL, so the worker's own start does not
        re-stamp the claim with the registry's generic default.
        """
        worker_name, env_overrides = _normalize_worker_selection(
            self.worker_selector(task)
        )
        worker_function = self._get_worker_function(worker_name)
        executor_metadata = await self._metadata_for_worker(worker_name)
        if self.worker_reports_lifecycle:
            build_id = get_current_build_id()
            if build_id is not None:
                env_overrides = {
                    **(env_overrides or {}),
                    STARDAG_BUILD_ID_ENV: str(build_id),
                    STARDAG_MODAL_APP_NAME_ENV: self.modal_app_name,
                }
                ttl_seconds = claim_ttl_seconds(task, self)
                if ttl_seconds is not None:
                    env_overrides[STARDAG_CLAIM_TTL_SECONDS_ENV] = str(ttl_seconds)
                if executor_metadata is not None:
                    for env_name, key in (
                        (STARDAG_MODAL_WORKSPACE_ENV, "workspace"),
                        (STARDAG_MODAL_ENVIRONMENT_ENV, "environment"),
                        (STARDAG_MODAL_FUNCTION_NAME_ENV, "function_name"),
                        (STARDAG_MODAL_APP_ID_ENV, "app_id"),
                        (STARDAG_MODAL_FUNCTION_ID_ENV, "function_id"),
                    ):
                        value = executor_metadata.get(key)
                        if value:
                            env_overrides[env_name] = value
                if self.reactive:
                    env_overrides[STARDAG_REACTIVE_ENV] = "1"
        return worker_function, env_overrides, executor_metadata

    def reports_lifecycle(self, task: BaseTask) -> bool:
        """Workers self-report lifecycle when enabled and a build is active."""
        active = self.worker_reports_lifecycle and get_current_build_id() is not None
        if active and not self._reports_lifecycle_logged:
            # Make the version-skew failure mode visible: nothing verifies
            # the deployed workers actually self-report. If the app was
            # deployed with an older stardag (or uses a custom run function
            # without lifecycle reporting), tasks will execute fine but sit
            # RUNNING in the registry with artifacts lost.
            self._reports_lifecycle_logged = True
            logger.info(
                "Engine-side lifecycle reporting is suppressed for "
                f"Modal-routed tasks (app {self.modal_app_name!r}): workers "
                "are assumed to self-report started/completed/suspended/"
                "failed events. If the deployed app predates worker "
                "self-reporting or uses a custom run function, pass "
                "ModalTaskExecutor(worker_reports_lifecycle=False) — "
                "otherwise tasks will appear stuck RUNNING in the registry."
            )
        return active

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        """Execute task on Modal (blocking remote call)."""
        try:
            worker_function, env_overrides, _ = await self._prepare_invocation(task)
            res = await worker_function.remote.aio(task, env_overrides=env_overrides)
            return res
        except Exception as e:
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )

    # --- Detached execution (spawn + re-attach + cancel) ---

    def supports_detached(self, task: BaseTask) -> bool:
        """Detached mode is per-executor (constructor flag), not per-task."""
        return self.detached

    def _make_handle(
        self,
        task: BaseTask,
        function_call: modal.FunctionCall,
        executor_metadata: dict[str, typing.Any] | None = None,
    ) -> DetachedHandle:
        """Wrap a FunctionCall in a DetachedHandle tracking in-flight state."""
        self._in_flight[task.id] = function_call

        async def wait() -> None | TaskStruct | TaskExecutionError:
            try:
                return await function_call.get.aio()
            except asyncio.CancelledError:
                # The build engine cancels the awaiting future on FAIL_FAST /
                # user cancellation. Unlike ``remote.aio``, cancelling
                # ``get()`` does NOT stop the detached remote call — cancel
                # it explicitly here. This must live in wait() (not only in
                # the executor cancel() hook): the finally below pops the
                # in-flight entry, and the asyncio cancellation typically
                # lands before the hook runs, so the hook would find nothing.
                # Shielded: this coroutine is already being cancelled.
                try:
                    await asyncio.shield(function_call.cancel.aio())
                except Exception as cancel_err:
                    logger.warning(
                        f"Failed to cancel Modal function call "
                        f"{function_call.object_id} for task {task.id} "
                        f"during cancellation: {cancel_err}"
                    )
                raise
            except Exception as e:
                return TaskExecutionError(
                    exception=e,
                    traceback="".join(tb_module.format_exception(e)),
                )
            finally:
                self._in_flight.pop(task.id, None)

        return DetachedHandle(
            executor=MODAL_EXECUTOR_NAME,
            ref=function_call.object_id,
            wait=wait,
            executor_metadata=executor_metadata,
        )

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        """Spawn the task on its worker function; return a re-attachable handle."""
        (
            worker_function,
            env_overrides,
            executor_metadata,
        ) = await self._prepare_invocation(task)
        function_call = await worker_function.spawn.aio(
            task, env_overrides=env_overrides
        )
        return self._make_handle(task, function_call, executor_metadata)

    async def reattach(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedHandle | None:
        """Re-attach to a spawned function call by id, if it is still live.

        Returns None (→ normal re-execution) when the ref belongs to a
        different backend, the call failed/was cancelled, or the result has
        expired. A call that already finished successfully yields a handle
        resolving immediately to its result.

        Known ambiguity (accepted): Modal's ``get(timeout=0)`` poll timeout
        raises the *builtin* ``TimeoutError`` (modal 1.5), and a task body
        that itself raised ``TimeoutError`` re-raises the same type — such
        a failed call classifies as still-running here. Not narrowable:
        ``modal.exception.TimeoutError`` is not what the poll raises, and
        ``FunctionCall.get_call_graph()`` does not reliably surface input
        status (verified live: stays PENDING after success/cancel). The
        re-attach path self-corrects: awaiting the returned handle's
        ``wait()`` re-raises the failure and the task is recorded failed.
        """
        if executor != MODAL_EXECUTOR_NAME or not self.detached:
            return None
        try:
            function_call = modal.FunctionCall.from_id(ref)
        except Exception:
            return None
        try:
            result = await function_call.get.aio(timeout=0)
        except TimeoutError:
            # Still running (or queued) — the interesting case. Base
            # metadata only: the worker function behind a bare ref isn't
            # known here.
            return self._make_handle(
                task, function_call, await self._get_base_executor_metadata()
            )
        except Exception:
            # Failed, cancelled, expired result, or unknown id — re-execute.
            return None

        async def resolved() -> None | TaskStruct | TaskExecutionError:
            return result

        return DetachedHandle(executor=MODAL_EXECUTOR_NAME, ref=ref, wait=resolved)

    async def detached_status(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedExecutionStatus:
        """Non-blocking probe of a spawned function call's state.

        Note: an expired result (>~7 days) and an unknown id also classify
        as FAILED — callers (scheduler ticks) check target existence first,
        so a successfully-finished-long-ago execution is already resolved
        as complete before this is consulted.

        Known ambiguity (accepted, see also ``reattach``): the poll timeout
        is the builtin ``TimeoutError``, indistinguishable from a task body
        that raised ``TimeoutError`` — such a failed execution classifies
        as RUNNING here. In practice the worker's own TASK_FAILED report
        resolves the task first; only a registry-less worker raising
        builtin TimeoutError hits the ambiguity.
        """
        if executor != MODAL_EXECUTOR_NAME or not self.detached:
            return DetachedExecutionStatus.UNKNOWN
        try:
            function_call = modal.FunctionCall.from_id(ref)
            await function_call.get.aio(timeout=0)
        except TimeoutError:
            return DetachedExecutionStatus.RUNNING
        except Exception as e:
            # Transient transport/infrastructure errors must NOT classify as
            # FAILED: callers (claim loser-resolution, tick healing) treat
            # FAILED as proof of death — a network blip mistaken for a dead
            # winner would let a racer record a live execution as failed and
            # spawn the duplicate the claim exists to prevent. UNKNOWN is
            # the safe degradation (leave/wait and re-probe later).
            if _is_transient_modal_error(e):
                logger.warning(
                    f"Transient error probing Modal call {ref!r}; treating "
                    f"as unknown: {e}"
                )
                return DetachedExecutionStatus.UNKNOWN
            return DetachedExecutionStatus.FAILED
        return DetachedExecutionStatus.SUCCEEDED

    async def cancel_detached(self, task: BaseTask, executor: str, ref: str) -> None:
        """Cancel a spawned function call by its recorded id."""
        if executor != MODAL_EXECUTOR_NAME:
            return
        try:
            await modal.FunctionCall.from_id(ref).cancel.aio()
        except Exception as e:
            logger.warning(
                f"Failed to cancel Modal function call {ref!r} for task {task.id}: {e}"
            )

    async def cancel(self, task: BaseTask) -> None:
        """Cancel the tracked in-flight function call for ``task``, if any."""
        function_call = self._in_flight.pop(task.id, None)
        if function_call is None:
            return
        try:
            await function_call.cancel.aio()
        except Exception as e:
            logger.warning(
                f"Failed to cancel Modal function call "
                f"{function_call.object_id} for task {task.id}: {e}"
            )

    async def setup(self) -> None:
        """No setup needed for Modal executor."""
        pass

    async def teardown(self) -> None:
        """No teardown needed for Modal executor."""
        pass


# --- Build and run function protocols ---


class BuildFailedError(Exception):
    """Raised when a build completes with failures or pending tasks."""

    pass


class BuildFunction(typing.Protocol):
    """Protocol for the function registered as the Modal "build" function.

    This function is called remotely on Modal to orchestrate a DAG build.
    It receives one or more root tasks, a worker selector, the Modal app
    name, and an optional ``build_kwargs`` dict, then coordinates task
    execution across Modal worker functions.

    Args (of ``__call__``):
        tasks: A single root ``BaseTask`` or a sequence of root tasks.
        worker_selector: Function picking a worker name per task.
        app_name: Name of the Modal app hosting the worker functions.
        build_kwargs: Optional dict of extra kwargs forwarded to the
            underlying build engine (the default ``Builder`` splats them
            into :func:`stardag.build`). ``None`` means "no extra kwargs".

    The default implementation (``Builder``) creates a ``ModalTaskExecutor``
    and calls ``stardag.build()``. Custom implementations can subclass
    ``Builder`` to override ``setup()``/``teardown()``/``build()``, or
    implement this protocol directly for full control.

    Any module-level code in the module where a custom build function is
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).
    """

    def __call__(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None: ...


class RunFunction(typing.Protocol):
    """Protocol for the function registered as Modal "worker_*" functions.

    This function is called remotely on Modal to execute a single task.
    It receives a task instance and returns either ``None`` (task completed)
    or the ``TaskStruct`` yielded at the first incomplete dynamic-deps yield
    (which may include deps that are already complete — the caller is
    expected to filter if that matters). This enables idempotent
    re-execution: the build system schedules the yielded deps, then
    re-invokes the task. On re-execution the generator advances past
    previously-yielded batches whose deps are now complete.

    The default implementation (``Runner``) handles sync, async, and dynamic
    deps tasks. Custom implementations can subclass ``Runner`` to override
    ``setup()``/``teardown()``/``run()``, or implement this protocol directly.

    Any module-level code in the module where a custom run function is
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).

    Args (of ``__call__``):
        task: The task instance to execute.

    Implementations *may* additionally accept an optional
    ``env_overrides: dict[str, str] | None`` keyword argument (the framework
    always forwards it by keyword). When the ``worker_selector`` returns
    ``(worker_name, env_overrides)`` (see :data:`WorkerSelection`), the
    framework forwards those overrides to run functions that accept the
    parameter; for run functions written against the older ``(task)``-only
    signature the framework instead applies the overrides to the process
    environment around the call. The default :class:`Runner` accepts
    ``env_overrides`` and applies them around its ``run`` call.
    """

    def __call__(self, task: BaseTask) -> None | TaskStruct: ...


class _RunFunctionWithEnv(typing.Protocol):
    """Internal protocol for run functions that accept ``env_overrides``.

    Used to type the call site that forwards selector-provided environment
    overrides (see :class:`StardagApp.finalize`'s ``_modal_run`` wrapper).
    """

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct: ...


# --- Default build/run implementations, with overridable methods ---


class Builder(BuildFunction):
    """Default builder implementation with overridable setup/teardown.

    Override ``setup()``/``teardown()``/``build()`` to customize behavior.
    Pass an instance to ``StardagApp(build_function=MyBuilder())``.

    Example:

    .. code-block:: python

        class MyBuilder(Builder):
            def setup(self, tasks):
                super().setup(tasks)
                configure_my_environment()

        stardag_app = StardagApp(
            "my-app",
            build_function=MyBuilder(),
            ...
        )
    """

    def __init__(self, *, detached: bool = True):
        """Initialize the builder.

        Args:
            detached: Execute tasks as detached spawned Modal function calls
                (restart-safe, re-attachable on resume, explicitly
                cancellable). False restores the legacy blocking
                ``remote`` calls. Passed to :class:`ModalTaskExecutor`.
        """
        self.detached = detached

    def setup(self, tasks: typing.Sequence[BaseTask] | BaseTask) -> None:
        """Optional setup logic before the build starts."""
        _setup_logging()

    def teardown(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        summary_or_exception: BuildSummary | None | Exception,
    ) -> None:
        """Optional teardown logic after the build finishes."""
        if isinstance(summary_or_exception, BuildSummary):
            summary = summary_or_exception
            logger.info(f"Build summary:\n{repr(summary)}")
            if summary.status != BuildExitStatus.SUCCESS:
                raise BuildFailedError(
                    f"Build finished with status {summary.status.value}: "
                    f"{summary.task_count.failed} failed, "
                    f"{summary.task_count.pending} pending"
                )
        elif summary_or_exception is None:
            logger.info("Build completed without a BuildSummary.")
        else:
            logger.error(f"Build exception:\n{repr(summary_or_exception)}")

    def __call__(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        """Core build logic to orchestrate the DAG build."""
        modal_executor = ModalTaskExecutor(
            modal_app_name=app_name,
            worker_selector=worker_selector,
            detached=getattr(self, "detached", True),
        )
        summary_or_exception: BuildSummary | None | Exception = BuildFailedError(
            "Unknown error during build"
        )  # Placeholder for type checking, this should never be raised

        try:
            self.setup(tasks)
            summary = self.build(tasks, modal_executor, build_kwargs=build_kwargs)
            summary_or_exception = summary
            return summary
        except Exception as exception:
            summary_or_exception = exception
            raise
        finally:
            self.teardown(tasks, summary_or_exception)

    def build(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        task_executor: ModalTaskExecutor,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        """Default build logic using stardag.build() with the ModalTaskExecutor.

        ``build_kwargs`` are forwarded to :func:`stardag.build` (e.g. ``fail_mode``,
        ``register_all``, ``global_lock_config``). Conflicting keys (``tasks``,
        ``task_executor``) are reserved and rejected.
        """
        kwargs = dict(build_kwargs or {})
        for reserved in ("tasks", "task_executor"):
            if reserved in kwargs:
                raise TypeError(
                    f"build_kwargs must not contain reserved key '{reserved}'"
                )
        return build(tasks, task_executor=task_executor, **kwargs)


class _WorkerLifecycleReporter:
    """Reports a task's lifecycle events from inside a Modal worker.

    Created by :class:`Runner` when a build id was forwarded (see
    ``STARDAG_BUILD_ID_ENV``) and the container has a configured registry.
    Reporting from the worker makes the events independent of the
    orchestrator's lifetime: completion/failure land even if the build
    function died mid-await, and each (re-)invocation's TASK_STARTED
    carries its own function call id for re-attach.

    All reporting is best-effort: a registry hiccup must never fail a task
    whose actual work succeeded — failures are logged loudly and the
    engine-side self-heal (target-existence check on the next build) covers
    a lost completion event.
    """

    def __init__(
        self,
        registry: typing.Any,
        build_id: UUID,
        task: BaseTask,
        *,
        reactive: bool = False,
        app_name: str | None = None,
        executor_metadata: dict[str, typing.Any] | None = None,
        claim_ttl_seconds: int | None = None,
    ):
        self.registry = registry
        self.build_id = build_id
        self.task = task
        self.reactive = reactive
        self.app_name = app_name
        self.executor_metadata = executor_metadata
        self.claim_ttl_seconds = claim_ttl_seconds

    @classmethod
    def create(
        cls, task: BaseTask, env_overrides: dict[str, str] | None
    ) -> "_WorkerLifecycleReporter | None":
        def _get(key: str) -> str | None:
            return (env_overrides or {}).get(key) or os.environ.get(key)

        raw_build_id = _get(STARDAG_BUILD_ID_ENV)
        if not raw_build_id:
            return None
        try:
            build_id = UUID(raw_build_id)
        except ValueError:
            logger.warning(f"Invalid {STARDAG_BUILD_ID_ENV}: {raw_build_id!r}")
            return None
        registry = registry_provider.get()
        # Exact-type check: only the literal do-nothing default suppresses
        # reporting — NoOpRegistry *subclasses* may implement real behavior.
        if type(registry) is NoOpRegistry:
            return None
        app_name = _get(STARDAG_MODAL_APP_NAME_ENV)
        # Executor metadata forwarded by the orchestrator's executor (same
        # dict it records on its own starts). Values missing on older
        # orchestrators are simply omitted.
        executor_metadata: dict[str, typing.Any] = {"kind": MODAL_EXECUTOR_NAME}
        if app_name:
            executor_metadata["app_name"] = app_name
        for key, env_name in (
            ("workspace", STARDAG_MODAL_WORKSPACE_ENV),
            ("environment", STARDAG_MODAL_ENVIRONMENT_ENV),
            ("function_name", STARDAG_MODAL_FUNCTION_NAME_ENV),
            ("app_id", STARDAG_MODAL_APP_ID_ENV),
            ("function_id", STARDAG_MODAL_FUNCTION_ID_ENV),
        ):
            value = _get(env_name)
            if value:
                executor_metadata[key] = value
        # The orchestrator's derived claim TTL, if it sent one. Malformed
        # values are ignored rather than raised on: this is a bound on an
        # expiry, and no worker should fail to report its own start over it.
        raw_ttl = _get(STARDAG_CLAIM_TTL_SECONDS_ENV)
        try:
            ttl_seconds = int(raw_ttl) if raw_ttl else None
        except ValueError:
            logger.warning(f"Invalid {STARDAG_CLAIM_TTL_SECONDS_ENV}: {raw_ttl!r}")
            ttl_seconds = None
        return cls(
            registry,
            build_id,
            task,
            reactive=_get(STARDAG_REACTIVE_ENV) == "1",
            app_name=app_name,
            executor_metadata=executor_metadata,
            claim_ttl_seconds=ttl_seconds,
        )

    def _guard(self, fn: typing.Callable[[], None], what: str) -> None:
        try:
            fn()
        except Exception:
            logger.exception(
                f"Worker lifecycle report ({what}) failed for task {self.task.id}"
            )

    def started(self) -> None:
        def _do() -> None:
            ref: str | None = None
            try:
                ref = modal.current_function_call_id()
            except Exception:
                pass
            self.registry.task_start(
                self.build_id,
                self.task,
                executor=MODAL_EXECUTOR_NAME,
                executor_ref=ref,
                executor_metadata=self.executor_metadata,
                claim_ttl_seconds=self.claim_ttl_seconds,
            )

        self._guard(_do, "start")

    def completed(self) -> None:
        self._guard(
            lambda: self.registry.task_complete(self.build_id, self.task),
            "complete",
        )

        def _artifacts() -> None:
            artifacts = self.task.artifacts()
            if artifacts:
                self.registry.task_upload_artifacts(self.build_id, self.task, artifacts)

        self._guard(_artifacts, "artifacts")
        self._wake_scheduler()

    def suspended(self, task_struct: TaskStruct | None = None) -> None:
        if self.reactive and task_struct is not None:
            # No resident orchestrator to pick up the yielded deps: register
            # them (with their requires() subtrees), persist their pickles
            # for the scheduler, and record the dynamic edges — BEFORE the
            # suspend event, so the frontier is consistent when a tick runs.
            self._guard(
                lambda: self._register_dynamic_deps(task_struct), "dynamic-deps"
            )
        self._guard(
            lambda: self.registry.task_suspend(self.build_id, self.task),
            "suspend",
        )
        self._wake_scheduler()

    def failed(self, exception: Exception) -> None:
        self._guard(
            lambda: self.registry.task_fail(
                self.build_id, self.task, error_message=str(exception)
            ),
            "fail",
        )
        self._wake_scheduler()

    def _register_dynamic_deps(self, task_struct: TaskStruct) -> None:
        result = asyncio.run(
            discover_and_register_aio(self.registry, self.build_id, task_struct)
        )
        store = BuildTaskStore(self.build_id)
        # The trigger's pre-flight structurally cannot see dynamically
        # yielded deps — they don't exist until their parent runs — so the
        # coverage check is re-run here, on the app's patterns as published
        # by the deployed worker wrapper. Once per class per process: this
        # runs on every suspending worker invocation.
        #
        # The same elision applies (see StardagApp._persist_discovered_tasks):
        # without it the pickle-free property would hold only until a task
        # yielded its first dynamic dependency, and a build with dynamic
        # deps would still need target-root write access.
        patterns = declared_task_module_patterns()
        if patterns:
            uncovered = uncovered_task_classes(
                result.incomplete.values(), patterns, only_unwarned=True
            )
            if uncovered:
                logger.warning(
                    format_uncovered_message(
                        uncovered,
                        patterns,
                        remedy=(
                            "These were registered as dynamic dependencies, "
                            "so the trigger's pre-flight could not see them."
                        ),
                    )
                )
            plan = plan_pickle_elision(result.incomplete.values(), patterns)
            store.save_tasks(task for task, _ in plan.pickled)
            logger.info(
                f"Build {self.build_id} dynamic deps of task "
                f"{self.task.id}: {plan.summary()}"
            )
        else:
            store.save_tasks(result.incomplete.values())
        deps = flatten_task_struct(task_struct)
        self.registry.task_add_dependencies(
            self.build_id, self.task, deps, is_dynamic=True
        )

    def _wake_scheduler(self) -> None:
        """Reactive wake-up: flag the build dirty, then spawn a tick.

        Order matters: the flag is set *before* the spawn, so if the tick
        finds the scheduler lease held, the holder's linger re-check is
        guaranteed to observe the wake-up.
        """
        if not self.reactive:
            return
        self._guard(lambda: self.registry.build_notify(self.build_id), "notify")
        app_name = self.app_name
        if app_name is None:
            logger.warning(
                "Reactive build without an app name — cannot spawn a "
                "scheduler tick (relying on the watchdog)."
            )
            return

        def _spawn_tick() -> None:
            modal.Function.from_name(app_name=app_name, name="tick").spawn(
                build_id=str(self.build_id)
            )

        self._guard(_spawn_tick, "tick-spawn")


class Runner(RunFunction):
    """Default runner implementation with overridable setup/teardown.

    Override ``setup()``/``teardown()``/``run()`` to customize behavior.
    Pass an instance to ``StardagApp(run_function=MyRunner())``.

    Example:

    .. code-block:: python

        class MyRunner(Runner):
            def setup(self, task):
                super().setup(task)
                torch.cuda.set_device(0)

        stardag_app = StardagApp(
            "my-app",
            run_function=MyRunner(),
            ...
        )
    """

    def __init__(self, *, report_lifecycle: bool = True):
        """Initialize the runner.

        Args:
            report_lifecycle: Report the task's lifecycle events
                (started/completed/suspended/failed + artifacts) to the
                registry from inside the worker, when a build id was
                forwarded by the executor and the container has registry
                credentials. See :class:`_WorkerLifecycleReporter`.
        """
        self.report_lifecycle = report_lifecycle

    def setup(self, task: BaseTask) -> None:
        """Optional setup logic before the task runs."""
        _setup_logging()

    def teardown(self, task: BaseTask, exception: Exception | None) -> None:
        """Optional teardown logic after the task runs."""
        if exception:
            logger.error(f"Task {repr(task)} raised an exception: {repr(exception)}")

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct:
        """Core logic to execute a single task.

        Returns ``None`` when the task completed, or a ``TaskStruct`` of
        dynamic dependencies that were not yet complete (idempotent
        re-execution pattern — see ``run``).

        Args:
            task: The task instance to execute.
            env_overrides: Optional environment variable overrides (selected by
                the ``worker_selector`` — see :data:`WorkerSelection`). When
                provided, they are set temporarily around the ``run`` call and
                the previous environment is restored afterwards.
        """
        # getattr: tolerate subclasses overriding __init__ without super()
        result: None | TaskStruct = None
        exception: Exception | None = None
        try:
            self.setup(task)
            # All lifecycle reporting happens inside the env-overrides
            # context, so overrides carrying environment-sensitive config
            # apply to reporting exactly as they do to run(). (Caveat:
            # stardag's config/registry providers cache on first access —
            # registry connection settings should come from the container's
            # process environment, i.e. deployment secrets, not overrides.)
            with temp_env_vars(env_overrides or {}):
                # getattr: tolerate subclasses overriding __init__ w/o super()
                reporter: _WorkerLifecycleReporter | None = None
                if getattr(self, "report_lifecycle", True):
                    try:
                        reporter = _WorkerLifecycleReporter.create(task, env_overrides)
                    except Exception:
                        # Best-effort contract covers creation too: a broken
                        # registry config must not fail a task before it runs.
                        logger.exception(
                            "Worker lifecycle reporter creation failed; "
                            "running without lifecycle reporting."
                        )
                if reporter is not None:
                    reporter.started()
                try:
                    result = self.run(task)
                except Exception as e:
                    if reporter is not None:
                        reporter.failed(e)
                    raise
                if reporter is not None:
                    if result is None:
                        reporter.completed()
                    else:
                        reporter.suspended(result)
        except Exception as e:
            exception = e
            raise
        finally:
            self.teardown(task, exception)
        return result

    def run(self, task: BaseTask) -> None | TaskStruct:
        """Default run logic — handles sync, async, and dynamic deps tasks.

        Dispatch policy:

        - **Async-only** (``run_aio`` defined, ``run`` not overridden):
          async generator ``run_aio`` is driven via ``_drive_async_generator``;
          otherwise ``asyncio.run(task.run_aio())``.
        - **Sync-only and dual** (``run`` defined, with or without ``run_aio``):
          ``task.run()`` is called. If it returns a sync generator it is
          driven via ``_drive_sync_generator``. Dual tasks intentionally
          prefer the sync path here because the Modal worker invocation is
          itself synchronous — if you need async execution for a dual task,
          implement it in ``run()`` (e.g. via ``asyncio.run`` internally).

        Generators cannot be serialized across the Modal boundary, so we
        mirror ``_run_task_in_process``: drive forward while yielded batches
        are fully complete, and at the first yield with any incomplete dep
        return the entire yielded ``TaskStruct``. The ``ModalTaskExecutor``
        builds those deps (filtering for incomplete ones) and re-invokes this
        function — on re-execution the generator advances past the
        now-complete batch.
        """
        has_run_aio = _has_custom_run_aio(task)
        has_run = _has_custom_run(task)

        if has_run_aio and not has_run:
            # Async-only task
            if inspect.isasyncgenfunction(type(task).run_aio):
                return asyncio.run(_drive_async_generator(task))
            asyncio.run(task.run_aio())
            return None

        # Sync (or dual) task — run and drive generator if returned.
        # Dual tasks deliberately take the sync path; see method docstring.
        return _drive_sync_generator(task.run())


def _drive_sync_generator(
    result: None | typing.Generator[TaskStruct, None, None] | TaskStruct,
) -> None | TaskStruct:
    """Drive a sync generator result for idempotent re-execution.

    Advances the generator past yield batches whose deps are all complete.
    Stops at the first yield with any incomplete dep and returns that yield's
    full ``TaskStruct`` (which may also include already-complete deps — the
    caller is expected to filter for incomplete ones when scheduling). If
    the generator completes, returns ``None``.

    If ``result`` is ``None`` (no dynamic deps) returns ``None``. If
    ``result`` is already a ``TaskStruct`` (unusual but possible when a
    user's ``run()`` returns deps directly) it is returned as-is.
    """
    if result is None:
        return None
    if not hasattr(result, "__next__"):
        # Already a TaskStruct (unusual, but handle it)
        return typing.cast(TaskStruct, result)

    gen = typing.cast(typing.Generator[TaskStruct, None, None], result)
    try:
        while True:
            yielded = next(gen)
            deps = flatten_task_struct(yielded)
            incomplete = [dep for dep in deps if not dep.complete()]
            if incomplete:
                return tuple(deps)
    except StopIteration:
        return None


async def _drive_async_generator(task: BaseTask) -> None | TaskStruct:
    """Drive an async generator ``run_aio`` for idempotent re-execution.

    Same contract as ``_drive_sync_generator``: advances past fully-complete
    yield batches and returns the first batch that contains any incomplete
    dep as a ``TaskStruct`` (may include already-complete deps). Returns
    ``None`` when the generator finishes.
    """
    agen = typing.cast(
        typing.AsyncGenerator[TaskStruct, None],
        task.run_aio(),  # type: ignore[assignment]
    )
    try:
        async for yielded in agen:
            deps = flatten_task_struct(yielded)
            incomplete = [dep for dep in deps if not dep.complete()]
            if incomplete:
                return tuple(deps)
    except StopAsyncIteration:
        pass
    return None


_default_build = Builder()
_default_run = Runner()


class PrefectBuilder(Builder):
    """Builder that uses Prefect for build orchestration.

    Requires the ``stardag.integration.prefect`` package to be installed.
    """

    def __init__(
        self,
        on_complete_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
        | None = None,
        before_run_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
        | None = None,
    ):
        # Prefect's build flow drives the executor via submit() only, so
        # detached mode has no effect there — keep the legacy behavior.
        super().__init__(detached=False)
        self.on_complete_callback = on_complete_callback
        self.before_run_callback = before_run_callback

    def build(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        task_executor: ModalTaskExecutor,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        if prefect_build_flow is None:
            raise ImportError("Prefect is not installed")

        import asyncio

        _flow = prefect_build_flow  # local for pyright narrowing

        # TODO: support multiple root tasks in PrefectBuilder
        if isinstance(tasks, BaseTask):
            task = tasks
        else:
            if len(tasks) != 1:
                raise ValueError(
                    f"PrefectBuilder currently supports only a single root task, "
                    f"got {len(tasks)}"
                )
            task = tasks[0]

        flow_kwargs = dict(build_kwargs or {})
        # ``task`` is reserved because the flow is invoked as
        # ``_flow(...)(task, ...)`` below — letting the user pass another
        # ``task`` via build_kwargs would surface as a confusing
        # "got multiple values for argument 'task'" TypeError.
        for reserved in (
            "task",
            "task_executor",
            "before_run_callback",
            "on_complete_callback",
        ):
            if reserved in flow_kwargs:
                raise TypeError(
                    f"build_kwargs must not contain reserved key '{reserved}'"
                )

        async def _run():
            await _flow.with_options(
                name=f"stardag-build-{task.get_namespace()}:{task.get_name()}"
            )(
                task,
                task_executor=task_executor,
                before_run_callback=(self.before_run_callback or create_markdown),
                on_complete_callback=(
                    self.on_complete_callback or upload_task_on_complete_artifacts
                ),
                **flow_kwargs,
            )

        asyncio.run(_run())
        # Prefect manages its own flow result; no BuildSummary available.
        return None


def _setup_logging():
    """Setup logging for the modal app."""
    logging.basicConfig(level=logging.INFO)


# --- Stardag App ---


def _infer_task_module_patterns(_depth: int = 2) -> tuple[str, ...]:
    """Infer ``task_modules`` from the module that constructs the app.

    The default declaration is "the root package of the module defining
    this app, recursively" — which is right far more often than not: an
    app and the tasks it schedules almost always live in the same
    distribution, and a whole-package wildcard costs only import time.

    Inference is impossible for a module that is not part of a package —
    ``__main__``, or a loose script that Modal loads as a top-level module.
    Such a module isn't importable in a container under a stable name in
    the first place, so a pattern derived from it would be a lie. We warn
    and opt out (the pickle path still works), rather than baking in a
    module list that would fail to import in every tick container.

    Args:
        _depth: Stack frames back to the user's call site (``__init__``'s
            caller by default). Not part of the public contract.
    """
    # Frames hold their locals and globals alive and participate in
    # reference cycles, so the walk is scoped and the references dropped
    # rather than left for the collector — this runs in long-lived
    # scheduler containers.
    frame = inspect.currentframe()
    try:
        for _ in range(_depth):
            frame = frame.f_back if frame is not None else None
        module_name = frame.f_globals.get("__name__") if frame is not None else None
        package = frame.f_globals.get("__package__") if frame is not None else None
    finally:
        del frame
    if not module_name or module_name == "__main__" or not package:
        logger.warning(
            "Could not infer StardagApp(task_modules=...): the app is "
            f"defined in {module_name or 'an unknown module'!r}, which is "
            "not part of an importable package. Reactive scheduler ticks "
            "will therefore fall back to the build task store's pickles "
            "(which need target-root write access at trigger time and are "
            "invalidated by a redeploy). Declare the modules explicitly — "
            'e.g. task_modules=["my_pkg.tasks.*"] — to let ticks '
            "reconstruct tasks from registry data instead, or pass "
            "task_modules=[] to silence this warning."
        )
        return ()
    return (f"{module_name.split('.')[0]}.*",)


class StardagApp:
    """Wrapper around modal.App for Stardag task execution.

    StardagApp manages the Modal app and its functions for building and
    running Stardag tasks. It supports deferred function creation to allow
    runtime configuration (e.g., profile-based environment variables).

    Lifecycle:
        1. Create StardagApp with settings (functions NOT created yet)
        2. Call finalize() to create the Modal functions (done by CLI on deploy)
        3. After deployment, use build_spawn/build_remote to execute tasks

    Example:
        import modal
        from stardag.integration.modal import StardagApp, FunctionSettings

        # User defines their image with full control
        image = (
            modal.Image.debian_slim()
            .pip_install("stardag", "pandas")
            .add_local_python_source("my_code")
        )

        # Create app (deferred - no functions created yet)
        stardag_app = StardagApp(
            "my-app",
            builder_settings=FunctionSettings(image=image),
            worker_settings={"default": FunctionSettings(image=image)},
        )

        # Deploy with CLI (finalize is called automatically):
        # $ stardag modal deploy my_app.py --profile production

        # After deployment, run tasks:
        stardag_app.build_spawn(my_task)

    Attributes:
        modal_app: The underlying modal.App instance.
        name: The app name.
        is_finalized: Whether finalize() has been called.
        task_modules: The validated task-module patterns (see the
            ``task_modules`` argument); empty when opted out.
        require_pickle_free: Whether a reactive trigger refuses to fall
            back to writing task pickles.
    """

    def __init__(
        self,
        modal_app_or_name: modal.App | str,
        *,
        build_function: BuildFunction = _default_build,
        run_function: RunFunction = _default_run,
        builder_settings: FunctionSettings,
        worker_settings: dict[str, FunctionSettings],
        worker_selector: WorkerSelector | None = None,
        tick_settings: FunctionSettings | None = None,
        watchdog_period_minutes: int | None = None,
        limit_key_selector: typing.Callable[[BaseTask], typing.Sequence[str]]
        | None = None,
        task_modules: typing.Sequence[str] | None = None,
        require_pickle_free: bool = False,
        modal_workspace: str | None = None,
        stardag_api_key_secret: "modal.Secret | str | None" = "stardag-api-key",
    ):
        """Initialize a StardagApp.

        Args:
            modal_app_or_name: Either a modal.App instance or a string name.
                If a string, a new modal.App will be created with that name.
            build_function: Callable registered as the Modal "build" function.
                Must match the ``BuildFunction`` protocol:
                ``(tasks, worker_selector, app_name) -> BuildSummary | None``.
                Defaults to ``Builder()`` which provides overridable
                ``setup()``/``teardown()``/``build()`` hooks. Subclass
                ``Builder`` for customization, or implement the protocol
                directly.

                Any module-level code in the module where this callable is
                defined will run inside the Modal container at import time —
                use this for container-level setup.
            run_function: Callable registered as the Modal worker functions.
                Must match the ``RunFunction`` protocol:
                ``(task) -> None``.
                Defaults to ``Runner()`` which provides overridable
                ``setup()``/``teardown()`` hooks.

                Any module-level code in the module where this callable is
                defined will run inside the Modal container at import time —
                use this for worker-level setup (GPU init, library preloading).
            builder_settings: Settings for the "build" function. Each
                function's settings are independent — nothing is propagated
                between them, except ``stardag_api_key_secret`` (below) and
                the deploy-time env config the CLI injects.
            worker_settings: Dict of worker name to settings. Must include
                "default". Fully independent per worker.
            worker_selector: Function to select the worker name for each task.
                Defaults to always returning "default".
            tick_settings: Settings for the reactive-scheduling ``tick`` /
                ``tick_watchdog`` functions. Defaults to ``builder_settings``
                when not given.
            watchdog_period_minutes: If set, register a scheduled watchdog
                that periodically re-ticks running reactive builds (the
                safety net for lost wake-ups, UI-cancelled builds, and stale
                concurrency-limit slots). Strongly recommended when using
                ``build_trigger(reactive=True)``. Default: no watchdog.
            limit_key_selector: Maps a task to the named registry
                concurrency-limit keys it runs under in reactive scheduling
                (deployed-app configuration applied by every tick). Default:
                no limits.
            task_modules: Modules whose import registers the task classes
                this app may schedule. **Only reactive scheduling needs
                this**: a scheduler tick reconstructs tasks from registry
                data and can resolve only classes that are already
                registered in its process (registration happens at class
                definition time). Resident builds hold the real task
                objects and are entirely unaffected.

                Each entry is an exact module (``"my_pkg.tasks.ingest"``)
                or a package with a trailing recursive wildcard
                (``"my_pkg.tasks.*"``); anything else raises. The patterns
                are expanded to a concrete module list at ``finalize()``
                and baked into the deployed tick, so **adding or moving
                task classes requires a redeploy**. Declared modules become
                import-hot — they are imported in every tick container —
                so keep heavy runtime dependencies inside ``run()`` rather
                than at module scope.

                Default (``None``): infer ``"<root package of the module
                defining this app>.*"``. Pass ``[]`` to opt out.

                **Declaring this explicitly is also the opt-in to skipping
                pickles** — the inferred default only drives the coverage
                warning. The trigger reads the local app definition while
                the tick runs the deployed one, so if inference alone
                elided, upgrading stardag would start dropping pickles that
                an app deployed by an older version cannot compensate for.
            require_pickle_free: Turn the pickle fallback from a silent
                safety net into a hard error. With ``task_modules``
                covering a build's classes, a reactive trigger writes no
                task pickles at all and therefore needs no target-root
                *write* access; with this flag, a trigger that *would* have
                fallen back to pickling raises instead, naming every task
                and why. Off by default (the fallback is what keeps the
                feature additive).

                Enforced at the trigger only. Dynamic dependencies
                registered from inside a worker apply the same elision but
                never raise: their task has already run, and failing its
                bookkeeping to enforce a storage preference would be a
                strictly worse outcome than one extra pickle.
            modal_workspace: Explicit Modal workspace name recorded in the
                executor metadata of triggered builds and started tasks
                (used by the UI for Modal dashboard deep links). Default:
                resolved once from the configured Modal token, best-effort.
            stardag_api_key_secret: The Modal secret carrying the Stardag
                Registry API key, injected into **every** function (build,
                workers, tick, watchdog) — all of them talk to the registry
                (workers self-report their lifecycle). Accepts a
                ``modal.Secret``, a secret *name* (``str``, resolved lazily
                via ``modal.Secret.from_name``), or ``None``.

                The default ``"stardag-api-key"`` is the secret name created
                by the CLI: run ``stardag modal stardag-api-key create`` to
                mint a Stardag API key and sync it into a Modal secret of
                that name (see the Modal how-to). With the secret in place,
                this default works out of the box; a string is used (rather
                than a ``modal.Secret``) so resolution is deferred to deploy
                time. If the named secret does not exist, ``finalize()``
                raises a clear error. Set to ``None`` if you supply the API
                key another way (a custom secret per function, or a
                non-secret mechanism).
        """
        if isinstance(modal_app_or_name, str):
            self.modal_app = modal.App(name=modal_app_or_name)
        else:
            assert isinstance(modal_app_or_name, modal.App)
            assert modal_app_or_name.name is not None
            self.modal_app = modal_app_or_name

        self.worker_selector = worker_selector or _default_worker_selector
        self._build_function = build_function
        self._run_function = run_function
        self._builder_settings = builder_settings
        self._worker_settings = worker_settings
        # Reactive scheduling: the "tick" function's settings (defaults to
        # builder_settings) and the optional periodic watchdog sweep that
        # re-ticks running builds (covers lost wake-ups and externally
        # cancelled builds). Set watchdog_period_minutes when using
        # build_trigger(reactive=True).
        self._tick_settings = tick_settings
        self.watchdog_period_minutes = watchdog_period_minutes
        # Maps a task to the named concurrency-limit keys it runs under in
        # reactive scheduling (see the registry's environment concurrency
        # limits). Deployed-app configuration — captured by the tick
        # function at finalize() so every tick of every build applies it
        # consistently (callables can't be persisted in the JSON build
        # meta like the scalar tick_kwargs).
        self.limit_key_selector = limit_key_selector
        # Task-module declaration for reactive scheduling: the patterns
        # whose expansion is imported by every scheduler tick so it can
        # rebuild task objects from registry data (see
        # stardag.build._task_modules). Validated eagerly — a malformed
        # pattern must fail here, not silently match nothing and surface
        # hours later as a tick that cannot reconstruct a task.
        #
        # Whether the patterns were *declared* or merely inferred decides
        # whether pickles may be elided — see _persist_discovered_tasks.
        # Inference must stay observation-only: it happens on every app,
        # including apps written before this feature existed.
        self._task_modules_declared = task_modules is not None
        if task_modules is None:
            task_modules = _infer_task_module_patterns()
        self.task_modules: tuple[str, ...] = validate_task_module_patterns(task_modules)
        if require_pickle_free and not self.task_modules:
            raise TaskModulesError(
                "require_pickle_free=True is meaningless without "
                "task_modules: with no declared modules, no task can be "
                "reconstructed from registry data and every task would "
                "need a pickle. Declare task_modules explicitly (if you "
                "left it at the default, inference was not possible — see "
                "the warning above), or drop require_pickle_free."
            )
        self.require_pickle_free = require_pickle_free
        # Explicit Modal workspace name for executor metadata (UI deep
        # links). Default: resolved from the Modal token, best-effort.
        # Used by build_trigger and by the tick's executor; the resident
        # build function's executor resolves it in-container instead.
        self.modal_workspace = modal_workspace
        # The registry-API-key secret, injected into every function at
        # finalize(). A string is resolved lazily to modal.Secret.from_name
        # (default defers resolution to deploy time); its existence is
        # validated in finalize() with a clear error. None disables it.
        if isinstance(stardag_api_key_secret, str):
            self._api_key_secret_name: str | None = stardag_api_key_secret
            self.stardag_api_key_secret: "modal.Secret | None" = modal.Secret.from_name(
                stardag_api_key_secret
            )
        else:
            self._api_key_secret_name = None
            self.stardag_api_key_secret = stardag_api_key_secret
        self._is_finalized = False

    @property
    def is_finalized(self) -> bool:
        """Whether the app has been finalized (functions created)."""
        return self._is_finalized

    @property
    def name(self) -> str:
        """The Modal app name."""
        assert self.modal_app.name is not None
        return self.modal_app.name

    @staticmethod
    def _prepare_function_settings(
        settings: FunctionSettings,
        *,
        extra_secrets: list[modal.Secret],
        auto_volumes: dict[str, modal.Volume],
    ) -> dict[str, typing.Any]:
        """Merge extra secrets and auto-volumes into function settings.

        Auto-mounted volumes are merged with user volumes, where user-specified
        volumes at the same mount path take precedence over auto-mounted ones.
        Secrets are de-duplicated by name (a named secret propagated from the
        builder plus one the function already declares should apply once).
        """
        result: dict[str, typing.Any] = dict(settings)

        # Merge secrets: existing + extra, de-duplicated by name.
        existing_secrets: list[modal.Secret] = list(result.get("secrets") or [])
        result["secrets"] = _dedupe_secrets(existing_secrets + extra_secrets)

        # Merge volumes: auto-mounted (lower priority) + user (higher priority)
        user_volumes = dict(result.get("volumes") or {})
        result["volumes"] = {**auto_volumes, **user_volumes}

        return result

    def finalize(
        self,
        *,
        extra_secrets: list[modal.Secret] | None = None,
        create_volumes_if_missing: bool = True,
    ) -> FinalizeResult:
        """Finalize the app by creating Modal functions.

        This method creates the builder and worker functions on the Modal app.
        It should be called before deployment, typically by the CLI.

        Discovered Modal volumes from target roots are automatically mounted at
        /mnt/stardag-volumes/<volume-name> and the STARDAG_MODAL_VOLUME_MOUNTS
        env var is set so that ModalMountedVolumeFileTarget (local I/O) is used
        instead of ModalVolumeRemoteFileSystem (API-based).

        Args:
            extra_secrets: Additional secrets to inject into all functions.
                This is where profile-based environment variables are injected.
            create_volumes_if_missing: Whether to create Modal volumes for
                target roots if they don't exist.

        Returns:
            FinalizeResult with created volumes, function names, mount info,
            and the expanded ``task_modules`` baked into the tick.

        Raises:
            RuntimeError: If finalize() has already been called.
            TaskModulesError: If a ``task_modules`` pattern cannot be
                expanded (e.g. its root package is not importable here).
        """
        if self._is_finalized:
            raise RuntimeError("StardagApp has already been finalized")

        # Discover and create Modal volumes from target roots
        target_roots_volumes = get_target_roots_volumes(
            create_if_missing=create_volumes_if_missing
        )

        # Compute auto-mount mapping from discovered volumes
        # volume_mounts: mount_path -> volume_name (for env var)
        # auto_volumes: mount_path -> Volume (for Modal function)
        volume_mounts: dict[str, str] = {}
        auto_volumes: dict[str, modal.Volume] = {}
        for vol_name, vol in target_roots_volumes.by_volume_name.items():
            mount_path = str(get_default_volume_mount_path(vol_name))
            volume_mounts[mount_path] = vol_name
            auto_volumes[mount_path] = vol

        extra_secrets = list(extra_secrets or [])

        # Inject volume mount config as env var so ModalMountedVolumeFileTarget is used
        if volume_mounts:
            extra_secrets.append(
                modal.Secret.from_dict(
                    {"STARDAG_MODAL_VOLUME_MOUNTS": json.dumps(volume_mounts)}
                )
            )
        # Bake the Modal workspace into every function's env. It's needed for
        # the UI's Modal dashboard deep links (executor metadata), but the
        # only way to resolve it — the Modal token — exists in this deploy
        # process, NOT in the deployed containers. Resolve it here (or use
        # the explicit override) and propagate it so containers don't have to
        # (and can't) look it up. Best-effort: if it can't be resolved, deep
        # links degrade gracefully (the UI shows env only).
        deploy_workspace = self.modal_workspace or _get_modal_workspace()
        if deploy_workspace:
            extra_secrets.append(
                modal.Secret.from_dict({STARDAG_MODAL_WORKSPACE_ENV: deploy_workspace})
            )
        # The registry API-key secret is injected into every function (build,
        # workers, tick, watchdog) — all of them talk to the registry. It's
        # the ONLY secret propagated across functions; per-function
        # `secrets` stay function-local. Validate a by-name secret's
        # existence with a clear error (best-effort: only a definitive
        # not-found errors out; no Modal context / auth just skips the check
        # so offline finalize and unit tests aren't broken).
        if self.stardag_api_key_secret is not None:
            if self._api_key_secret_name is not None:
                try:
                    self.stardag_api_key_secret.hydrate()
                except modal.exception.NotFoundError as e:
                    name = self._api_key_secret_name
                    secret_name_flag = (
                        "" if name == "stardag-api-key" else f" --secret-name {name}"
                    )
                    raise StardagError(
                        f"StardagApp.stardag_api_key_secret refers to a Modal "
                        f"secret named {name!r} that does not exist in the "
                        f"current Modal environment. Run "
                        f"`stardag modal stardag-api-key create"
                        f"{secret_name_flag}` to mint a Stardag API key and "
                        f"sync it into a Modal secret of that name, so the "
                        f"deployed functions can authenticate to the "
                        f"registry. If you supply the API key another way, or "
                        f"set it per function, pass stardag_api_key_secret="
                        f"None."
                    ) from e
                except Exception as e:  # noqa: BLE001 - best-effort validation
                    logger.debug(
                        f"Could not validate stardag_api_key_secret "
                        f"{self._api_key_secret_name!r} (no Modal context?); "
                        f"proceeding: {e}"
                    )
            extra_secrets.append(self.stardag_api_key_secret)
        # Expand the declared task-module patterns to a concrete, sorted
        # module list ONCE, here, and bake it into the deployed functions
        # below. Deploy-time expansion (rather than in-container) keeps the
        # deployed set explicit and auditable, keeps container startup off
        # the filesystem, and makes the deployment reproducible: a module
        # added after this deploy is not silently picked up by a running
        # tick — it needs a redeploy, which is also when the operator gets
        # to see the list change. Only name expansion happens here (no
        # submodule imports): the CLI does the optional local import check.
        task_module_patterns = self.task_modules
        task_modules = expand_task_module_patterns(task_module_patterns)
        # Wrap callables in real functions for Modal compatibility.
        # Modal's is_async() only accepts inspect.isfunction()-compatible objects,
        # not callable class instances. The wrappers delegate to the actual callable
        # and are what get serialized/sent to Modal.
        build_fn = self._build_function

        def _modal_build(
            tasks: typing.Sequence[BaseTask] | BaseTask,
            worker_selector: WorkerSelector,
            app_name: str,
            build_kwargs: dict[str, typing.Any] | None = None,
        ) -> BuildSummary | None:
            return build_fn(tasks, worker_selector, app_name, build_kwargs=build_kwargs)

        run_fn = self._run_function
        # The ``RunFunction`` protocol gained an optional ``env_overrides``
        # parameter. Older custom run functions implemented the protocol with a
        # bare ``(task)`` signature, so only forward ``env_overrides`` to those
        # that accept it; otherwise apply the overrides in the wrapper.
        run_fn_accepts_env = _callable_accepts_env_overrides(run_fn)

        def _modal_run(
            task: BaseTask, *, env_overrides: dict[str, str] | None = None
        ) -> typing.Any:
            # Publish the app's task-module patterns for the worker-side
            # code that needs them but is nowhere near the app object:
            # _WorkerLifecycleReporter._register_dynamic_deps checks the
            # coverage of dynamically yielded deps, which the trigger's
            # pre-flight cannot see. The worker does not IMPORT the
            # modules: its task arrived by value (self-importing) and its
            # dynamic deps were just constructed by user code, so their
            # classes are registered by definition.
            set_declared_task_module_patterns(task_module_patterns)
            if run_fn_accepts_env:
                run_fn_with_env = typing.cast(_RunFunctionWithEnv, run_fn)
                return run_fn_with_env(task, env_overrides=env_overrides)
            with temp_env_vars(env_overrides or {}):
                return run_fn(task)

        # Create builder function
        builder_settings = self._prepare_function_settings(
            self._builder_settings,
            extra_secrets=extra_secrets,
            auto_volumes=auto_volumes,
        )
        self.modal_app.function(
            **{
                **builder_settings,
                "name": "build",
                "serialized": True,
            }
        )(_modal_build)

        function_names = ["build"]

        # Create worker functions
        for worker_name, settings in self._worker_settings.items():
            worker_settings = self._prepare_function_settings(
                settings,
                extra_secrets=extra_secrets,
                auto_volumes=auto_volumes,
            )

            func_name = f"worker_{worker_name}"
            self.modal_app.function(
                **{
                    **worker_settings,
                    "name": func_name,
                    "serialized": True,
                }
            )(_modal_run)
            function_names.append(func_name)

        # Reactive scheduler tick (see stardag.build.run_tick_aio). Spawned
        # by build_trigger(reactive=True), by workers finishing tasks, and
        # by the optional watchdog below. Idempotent and single-flighted —
        # safe to invoke at any time; no-ops on non-reactive builds.
        app_name = self.name
        default_worker_selector = self.worker_selector
        limit_key_selector = self.limit_key_selector
        modal_workspace = self.modal_workspace
        # Per-worker Modal timeouts, captured here because this is the only
        # place they exist: the tick runs in a deployed container with no
        # access to the app object, and the claim TTL it records for a task
        # is derived from the timeout of the worker that task routes to.
        # Workers without an explicit timeout are simply absent (Modal's own
        # default is not a promise this SDK should encode).
        worker_timeouts = {
            worker_name: timeout
            for worker_name, settings in self._worker_settings.items()
            if (timeout := settings.get("timeout")) is not None
        }

        def _modal_tick(
            build_id: str,
            tick_kwargs: dict[str, typing.Any] | None = None,
        ) -> dict[str, typing.Any]:
            _setup_logging()
            import dataclasses
            from uuid import UUID as _UUID

            from stardag.build import BuildTaskStore as _BuildTaskStore
            from stardag.exceptions import NotFoundError as _NotFoundError
            from stardag.exceptions import (
                is_missing_route_error as _is_missing_route_error,
            )

            build_uuid = _UUID(build_id)
            task_store = _BuildTaskStore(build_uuid)
            registry = registry_provider.get()
            # The reactive marker/owner/config live in the registry (not on
            # the target root): read them with the lighter GET /builds/{id}
            # for this pre-lease gate — the full frontier is only fetched
            # once the tick actually processes it (run_tick_aio). Reactive
            # scheduling against a server predating the build_get shape 404s
            # only on a genuine missing route; a resource-level 404 (build
            # deleted) must propagate as a real not-found.
            try:
                build_info = registry.build_get(build_uuid)
            except _NotFoundError as e:
                if not _is_missing_route_error(e):
                    raise
                raise RuntimeError(
                    "The registry server does not support reactive "
                    "scheduling (build endpoint too old). Upgrade "
                    "stardag-api to a version matching this SDK."
                ) from e
            reactive_app_name = build_info.reactive_app_name
            if reactive_app_name is None:
                # Not a reactively-scheduled build (e.g. a resident-
                # orchestrator build swept by the watchdog): never schedule
                # on top of it, and don't even acquire the scheduler lease.
                logger.info(
                    f"Tick for build {build_id}: not reactively scheduled "
                    "(no reactive_app_name); skipping."
                )
                return {"outcome": "not_reactive"}
            # App ownership: with multiple StardagApps in one environment,
            # every app's watchdog sweeps ALL running reactive builds — but
            # only the app recorded at trigger time may drive a build.
            # A foreign app's tick would schedule with ITS commit (its
            # workers, its selectors) and unpickle the owning app's task
            # store (pickle skew across commits), so it must not run the
            # tick loop itself. Instead it FORWARDS: best-effort spawn of
            # the owner's tick, so wake-ups that land on the wrong app
            # (e.g. a still-running worker of the previous owner after a
            # takeover) are not dropped, and every app's watchdog sweep
            # doubles as cross-app coverage. The owner-side single-flight
            # lease collapses duplicate forwards. Explicit takeover =
            # re-trigger from the new app (updates reactive_app_name and
            # re-persists the task objects under the new code).
            owner_app = reactive_app_name
            if owner_app != app_name:
                forwarded = False
                try:
                    modal.Function.from_name(app_name=owner_app, name="tick").spawn(
                        build_id=build_id
                    )
                    forwarded = True
                except Exception as e:
                    # Owner app deleted/renamed: the build is orphaned —
                    # surfaced in logs; remedy is a re-trigger from a live
                    # app (see the how-to's app-ownership section).
                    logger.info(
                        f"Tick for build {build_id}: could not forward to "
                        f"owner app {owner_app!r} (deleted?): {e}"
                    )
                logger.info(
                    f"Tick for build {build_id}: owned by app "
                    f"{owner_app!r}, not {app_name!r}; "
                    f"{'forwarded to owner' if forwarded else 'skipping'}."
                )
                return {
                    "outcome": "foreign_app",
                    "owner_app": owner_app,
                    "forwarded": forwarded,
                }
            # Per-build tick configuration persisted at trigger time in the
            # registry — every tick (worker wake-ups and watchdog sweeps
            # spawn with only the build id) runs with the same settings.
            # Explicit tick_kwargs (tests/manual invocations) win over
            # persisted ones; the limit key selector is deployed-app config.
            config = _build_tick_config(
                build_info.reactive_tick_kwargs, tick_kwargs, limit_key_selector
            )

            # Register the app's task classes in THIS container before the
            # tick reconstructs anything: rehydrating a task from registry
            # data is a dict lookup in the polymorphic registry, which is
            # populated only as a side effect of importing the defining
            # modules (unlike pickle, which self-imports). The list was
            # expanded and frozen at deploy time; the import is cached per
            # module list, so a container serving many ticks pays it once.
            # Failures warn rather than abort — and are retained, so a
            # later "could not rehydrate" error can name them.
            if task_modules:
                set_declared_task_module_patterns(task_module_patterns)
                import_task_modules(task_modules)

            executor = ModalTaskExecutor(
                modal_app_name=app_name,
                worker_selector=default_worker_selector,
                reactive=True,
                modal_workspace=modal_workspace,
                worker_timeouts=worker_timeouts,
            )
            lock_manager = RegistryGlobalConcurrencyLockManager(
                # No waiting on the scheduler lease: a held lease means
                # another tick is active and will observe the wake-up flag.
                config=GlobalLockConfig(lock_wait_timeout_seconds=None),
            )
            summary = asyncio.run(
                run_tick_aio(
                    build_uuid,
                    registry=registry,
                    task_executor=executor,
                    lock_manager=lock_manager,
                    task_store=task_store,
                    config=config,
                )
            )
            logger.info(f"Tick for build {build_id}: {summary}")
            return dataclasses.asdict(summary)

        # tick/watchdog default to builder_settings when tick_settings is
        # not given; the api-key secret is in extra_secrets so they get
        # registry credentials regardless of which settings apply.
        tick_settings = self._prepare_function_settings(
            self._tick_settings or self._builder_settings,
            extra_secrets=extra_secrets,
            auto_volumes=auto_volumes,
        )
        self.modal_app.function(
            **{**tick_settings, "name": "tick", "serialized": True}
        )(_modal_tick)
        function_names.append("tick")

        if self.watchdog_period_minutes is not None:

            def _modal_tick_watchdog() -> None:
                _setup_logging()
                # Scoped to this app's own reactive builds: see
                # _run_watchdog_sweep for why sweeping the environment's
                # whole RUNNING set is both wasteful and unsafe at scale.
                _run_watchdog_sweep(
                    registry_provider.get(),
                    _modal_tick,
                    reactive_app_name=app_name,
                )

            self.modal_app.function(
                **{
                    **tick_settings,
                    "name": "tick_watchdog",
                    "serialized": True,
                    "schedule": modal.Period(minutes=self.watchdog_period_minutes),
                }
            )(_modal_tick_watchdog)
            function_names.append("tick_watchdog")

        self._is_finalized = True

        return FinalizeResult(
            volumes=target_roots_volumes.by_root_key,
            functions=function_names,
            volume_mounts=volume_mounts,
            auto_volumes=auto_volumes,
            task_modules=task_modules,
        )

    def build_spawn(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Spawn a build job on a deployed Modal app (non-blocking).

        This method looks up the deployed "build" function by name and spawns
        a new execution. Use this for fire-and-forget builds.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            A Modal FunctionCall handle for the spawned build.

        Example:
            handle = stardag_app.build_spawn(my_task)
            # Or multiple roots:
            handle = stardag_app.build_spawn([task_a, task_b])
            # Optionally wait for result:
            result = handle.get()
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def build_trigger(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
        build_id: UUID | None = None,
        description: str | None = None,
        reactive: bool = False,
        tick_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildTriggerResult:
        """Trigger a build with a registry build id minted at the trigger point.

        Unlike :meth:`build_spawn` — where the build id is created *inside*
        the Modal build container — this method first creates (or reuses) the
        build in the registry from the calling process, then spawns the
        deployed build function with ``resume_build_id`` set to it. As a
        result:

        - Any restart of the build function (Modal retry after preemption, a
          manual re-trigger with the returned ``build_id``) **resumes the
          same build** instead of creating a new one: already-completed task
          targets are detected during discovery and skipped.
        - The build appears in the registry immediately, before the Modal
          container has started.

        Set ``retries`` in the app's ``builder_settings`` to let Modal
        automatically re-run (and thereby resume) the build function after
        infrastructure failures.

        Requires registry credentials in the calling process (the active
        stardag profile), in addition to Modal credentials. If no registry is
        configured, use :meth:`build_spawn` instead.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build
                function (must not contain ``resume_build_id``; it is set by
                this method).
            build_id: Existing build id to re-attach to (e.g. from a previous
                ``build_trigger`` call). If None, a new build is created.
            description: Optional description for the new build (ignored when
                ``build_id`` is given).
            reactive: **Experimental.** Schedule the build reactively (no
                resident orchestrator): discovery runs here at the trigger,
                task objects are persisted to the build task store under the
                default target root, and short-lived scheduler *ticks*
                (spawned now, by workers finishing tasks, and by the optional
                watchdog) drive the build — see ``stardag.build.run_tick_aio``
                for semantics and current limitations. Requires the app to be
                deployed with this stardag version (the ``tick`` function and
                self-reporting workers), and registry + target-root access in
                the calling process. Re-trigger with the returned ``build_id``
                to wake a stalled build or add new root tasks to it.
            tick_kwargs: Optional kwargs for the reactive ``TickConfig``
                (e.g. ``{"linger_seconds": 30}``).

        Returns:
            BuildTriggerResult with the ``build_id`` and the spawned Modal
            ``FunctionCall`` handle (the build function, or the first
            scheduler tick when ``reactive=True``).
        """
        merged_kwargs = dict(build_kwargs or {})
        if "resume_build_id" in merged_kwargs:
            raise TypeError(
                "build_kwargs must not contain 'resume_build_id'; pass "
                "build_id=... to build_trigger instead"
            )
        if reactive and merged_kwargs:
            raise TypeError(
                "build_kwargs are not supported with reactive=True (there is "
                "no resident build function); use tick_kwargs for TickConfig "
                "options"
            )
        if reactive and worker_selector is not None:
            raise TypeError(
                "worker_selector overrides are not supported with "
                "reactive=True: later scheduler ticks (worker wake-ups, "
                "watchdog) always use the app's deployed worker_selector, so "
                "a per-trigger override would change routing mid-build. "
                "Configure the selector on StardagApp instead."
            )
        if reactive:
            tick_kwargs = _validate_tick_kwargs(tick_kwargs)

        registry = registry_provider.get()
        # A configured registry is needed to mint a new build id, and always
        # in reactive mode (discovery/registration runs at the trigger).
        if (build_id is None or reactive) and isinstance(registry, NoOpRegistry):
            raise RuntimeError(
                "build_trigger requires a configured registry to mint the "
                "build id at the trigger point (run 'stardag auth login' "
                "or configure an API key). Use build_spawn to trigger a "
                "build without local registry credentials."
            )
        task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
        explicit_build_id = build_id is not None
        executor_metadata = self._build_executor_metadata(reactive=reactive)
        if build_id is None:
            build_id = registry.build_start(
                root_tasks=task_list,
                description=description,
                executor_metadata=executor_metadata,
            )

        if reactive:
            if self.watchdog_period_minutes is None:
                logger.warning(
                    "Reactive build triggered on an app without a watchdog "
                    "(watchdog_period_minutes is not set): a lost wake-up "
                    "(e.g. a silently dead worker, or a tick killed while "
                    "holding the scheduler lease) can stall the build until "
                    "it is manually re-triggered. Strongly recommended: "
                    "StardagApp(watchdog_period_minutes=5)."
                )
            return self._trigger_reactive(
                task_list,
                build_id=build_id,
                registry=registry,
                tick_kwargs=tick_kwargs,
                is_retrigger=explicit_build_id,
                executor_metadata=executor_metadata,
            )

        merged_kwargs["resume_build_id"] = build_id
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        function_call = build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=merged_kwargs,
        )
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def _build_executor_metadata(self, *, reactive: bool) -> dict[str, typing.Any]:
        """Build-level executor metadata for a trigger (best-effort)."""
        metadata: dict[str, typing.Any] = {
            "kind": MODAL_EXECUTOR_NAME,
            "app_name": self.name,
            "function_name": "tick" if reactive else "build",
            "reactive": reactive,
        }
        try:
            workspace = self.modal_workspace or _get_modal_workspace()
            if workspace:
                metadata["workspace"] = workspace
            environment = _get_modal_environment()
            if environment:
                metadata["environment"] = environment
        except Exception:
            logger.debug(
                "Failed to resolve Modal workspace/environment for "
                "build executor metadata",
                exc_info=True,
            )
        return metadata

    def _preflight_task_modules(self, tasks: typing.Iterable[BaseTask]) -> None:
        """Warn about discovered task classes ``task_modules`` doesn't cover.

        The trigger is the one place that holds both the real DAG and the
        app, so it is the only place that can catch a class no scheduler
        tick will be able to reconstruct — before a single container
        starts, rather than as a mystifying tick failure hours later.

        **Severity is a warning, not an error** (unless
        ``require_pickle_free``). An uncovered class is not broken: it
        falls back to the pickle path, which is exactly how every reactive
        build worked before ``task_modules`` existed. Failing the trigger
        would therefore break working setups the moment they upgrade,
        which is precisely what "this feature is additive" forbids. The
        hard failure lives behind ``require_pickle_free=True``, raised
        from the persistence step below (which additionally knows about
        round-trip failures, not just coverage).

        Known limitation: this reads the *local* app definition, so it
        cannot detect that the deployed app was built from an older
        ``task_modules``. Closing that needs the deployed list exposed for
        comparison.

        Skipped entirely when the app opted out of ``task_modules`` — an
        app that never declared any would otherwise warn about every class
        in every DAG, on every trigger.
        """
        if not self.task_modules:
            return
        uncovered = uncovered_task_classes(tasks, self.task_modules)
        if not uncovered:
            return
        logger.warning(
            format_uncovered_message(
                uncovered,
                self.task_modules,
                remedy=(
                    "Until then these tasks stay dependent on their "
                    "build-task-store pickles, which need target-root "
                    "write access and are invalidated by a redeploy."
                ),
            )
        )

    def _persist_discovered_tasks(
        self, build_id: UUID, tasks: typing.Iterable[BaseTask]
    ) -> None:
        """Write the build task store, skipping pickles that aren't needed.

        Unless the app *opted in*, this is byte-for-byte the old
        behaviour: pickle everything. With opt-in, each task gets a local
        dry run of what a tick will do — reconstruct it from exactly the
        payload registration stored — and only the ones that fail keep a
        pickle. A build whose classes are all covered writes nothing to
        the target root at all, so the trigger stops needing write access
        there (and stops being vulnerable to a storage error minting an
        orphan RUNNING build).

        Opt-in means ``task_modules`` was passed explicitly (or
        ``require_pickle_free`` was), NOT merely inferred. This matters:
        the trigger runs from the local app definition while the tick runs
        from the *deployed* one, and nothing lets the trigger see the
        deployed app's task modules (the stale-deploy blind spot). If
        inference alone enabled elision, then simply upgrading the SDK
        would start eliding pickles that an app deployed by an older SDK
        has no baked-in module list to compensate for — turning an upgrade
        into a broken build. Elision therefore requires an act by the user,
        which is also what makes the "redeploy after changing
        ``task_modules``" requirement land at a moment they are paying
        attention. Inference still drives the coverage warning, which is
        pure observation and safe to do everywhere.
        """
        store = BuildTaskStore(build_id)
        if not self.task_modules or not (
            self._task_modules_declared or self.require_pickle_free
        ):
            store.save_tasks(tasks)
            return
        plan: PickleElisionPlan = plan_pickle_elision(tasks, self.task_modules)
        if self.require_pickle_free:
            error = plan.require_pickle_free_error()
            if error is not None:
                raise TaskModulesError(error)
        store.save_tasks(task for task, _ in plan.pickled)
        logger.info(f"Build {build_id} task store: {plan.summary()}")

    def _trigger_reactive(
        self,
        task_list: list[BaseTask],
        *,
        build_id: UUID,
        registry: typing.Any,
        tick_kwargs: dict[str, typing.Any] | None,
        is_retrigger: bool,
        executor_metadata: dict[str, typing.Any] | None = None,
    ) -> BuildTriggerResult:
        """Reactive trigger: discover + persist here, then spawn the first tick.

        Re-triggering an existing build id is fully supported:

        - The build is *resumed* (BUILD_RESUMED) so a terminal build —
          including a FAILED one — becomes RUNNING again and ticks act on
          it (they bail on terminal statuses otherwise).
        - The passed roots are appended to the build's ``root_task_ids``
          server-side, so terminal detection covers them (previously,
          completion of the original roots would strand re-triggered
          subtrees silently).
        - Previously failed/cancelled/skipped tasks in the (re-)discovered
          DAG are reset to pending (``retry_failed``) — the retry path for
          reactive builds.
        - The reactive metadata is updated in the registry: because the
          registry is mutable (unlike an immutable target root), a
          re-trigger MAY now change ``tick_kwargs`` (a bare re-trigger with
          no explicit tick_kwargs preserves the existing ones); the roots
          live in the registry too (``build_add_roots`` above).

        ``tick_kwargs`` are persisted in the build's ``reactive_tick_kwargs``
        in the registry so that EVERY tick — including worker wake-ups and
        watchdog sweeps, which spawn with only the build id — runs with the
        same configuration.
        """
        root_ids = [str(t.id) for t in task_list]
        # Everything up to and including build_set_reactive_meta runs AFTER
        # the build row exists and is RUNNING (build status is derived from
        # events, so nothing else will ever move it). A failure in here —
        # most likely a target-root permission/storage error on the task
        # store write, but discovery can raise too — used to leave the build
        # RUNNING forever, and, because the reactive marker is written last,
        # not even attributable to an app: invisible to the owner's scoped
        # watchdog sweep and swept fruitlessly by everything else.
        #
        # Writing the marker earlier would make such a build discoverable,
        # but it would also expose a partially-discovered DAG to a tick,
        # which treats the registry frontier as ground truth and can
        # terminal a build whose tasks are not registered yet. So the
        # ordering stays and the terminal event is the fix: any failure
        # emits BUILD_FAILED naming the stage, then re-raises.
        stage = "build resume / root registration"
        # Emitting the terminal event is only correct once THIS trigger has
        # put the build into RUNNING. A fresh build was minted and started by
        # the caller, so it already is. A re-trigger's build may still be
        # terminal until build_resume succeeds — and a transient error on
        # that very call would otherwise flip a COMPLETED build to FAILED,
        # which is strictly worse than the orphan this wrapper exists to
        # prevent. A re-trigger of an already-RUNNING build is the same case
        # seen from the other side: it was RUNNING before we touched it, so
        # it is not an orphan of ours to terminate.
        build_is_running = not is_retrigger
        try:
            if is_retrigger:
                # Un-terminal the build (no-op on a fresh/running build) and
                # register the (possibly new) roots BEFORE discovery, so a
                # concurrent tick can't complete-and-terminal the build on
                # the old root set while we're adding to it.
                registry.build_resume(build_id, executor_metadata=executor_metadata)
                build_is_running = True
                registry.build_add_roots(build_id, root_ids)
            stage = "task discovery and registration"
            discovery = asyncio.run(
                discover_and_register_aio(
                    registry, build_id, tuple(task_list), retry_failed=True
                )
            )
            stage = "task-module coverage pre-flight"
            # Runs on the DISCOVERED incomplete set: those are exactly the
            # tasks a tick may have to rehydrate, and reusing discovery's
            # walk avoids a second local traversal of the DAG.
            self._preflight_task_modules(discovery.incomplete.values())
            stage = "task store write"
            self._persist_discovered_tasks(build_id, discovery.incomplete.values())
            stage = "reactive metadata write"
            # Persist the reactive marker/owner/config in the registry
            # (``reactive_app_name`` is the "this build is reactively
            # scheduled" marker read by every tick). This is an upsert:
            # because the registry is mutable — unlike a possibly-immutable
            # target root — a re-trigger MAY update tick_kwargs. tick_kwargs
            # is passed through as-is: None (a bare re-trigger) preserves the
            # stored config server-side rather than wiping it, so the 0.10.1
            # merge-semantics guarantee holds. Build roots are tracked in the
            # registry too (build_add_roots above — the scheduler reads them
            # from the frontier).
            registry.build_set_reactive_meta(
                build_id, app_name=self.name, tick_kwargs=tick_kwargs
            )
        except BaseException as error:
            # BaseException, not Exception: a Ctrl-C during discovery is one
            # of the likelier ways to abandon a trigger, and it wedges the
            # build exactly the same way. The original error always
            # propagates — _fail_build_on_trigger_error never masks it.
            if build_is_running:
                _fail_build_on_trigger_error(registry, build_id, stage, error)
            raise
        # The spawn is deliberately OUTSIDE the wrapper: by this point the
        # durable state is complete and consistent, and the build carries the
        # reactive marker, so a failed spawn is recovered by the app's next
        # watchdog sweep (or a re-trigger). Failing the build here would take
        # it out of the watchdog's RUNNING listing and remove that recovery.
        tick_function = modal.Function.from_name(app_name=self.name, name="tick")
        function_call = tick_function.spawn(build_id=str(build_id))
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def build_remote(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Run a build on a deployed Modal app (blocking).

        This method looks up the deployed "build" function by name and runs
        it synchronously. Use this when you need to wait for the build result.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            The result of the build.

        Example:
            result = stardag_app.build_remote(my_task)
            # Or multiple roots:
            result = stardag_app.build_remote([task_a, task_b])
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.remote(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def local_entrypoint(self, *args, **kwargs):
        """Create a local entrypoint on the underlying Modal app.

        This is a passthrough to modal.App.local_entrypoint().
        """
        return self.modal_app.local_entrypoint(*args, **kwargs)


class WorkerSelectorByName:
    """Worker selector that routes tasks based on task name.

    Example:
        selector = WorkerSelectorByName(
            name_to_worker={"heavy_task": "gpu", "io_task": "high_memory"},
            default_worker="default",
        )
        stardag_app = StardagApp(..., worker_selector=selector)
    """

    def __init__(self, name_to_worker: dict[str, str], default_worker: str):
        """Initialize the selector.

        Args:
            name_to_worker: Dict mapping task names to worker names.
            default_worker: Worker name to use for tasks not in the mapping.
        """
        self.name_to_worker = name_to_worker
        self.default_worker = default_worker

    def __call__(self, task: BaseTask) -> str:
        return self.name_to_worker.get(task.get_name(), self.default_worker)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.name_to_worker}, {self.default_worker})"
        )
