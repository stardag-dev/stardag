"""Executor metadata, and the env-var channel that carries it to workers.

Two things live here, because they are two halves of one mechanism:

- **The env-var names** through which an orchestrator (a resident build
  function or a scheduler tick) tells a Modal worker what build it is running
  for and what it should report about itself. They ride on the worker
  invocation's ``env_overrides``, which keeps the deployed worker function
  signature unchanged — an older deployed worker simply applies them as
  harmless env vars.
- **The resolution of the Modal coordinates** that metadata consists of
  (workspace, environment, app id, function id), all best-effort and cached:
  the UI turns them into Modal dashboard deep links, and nothing about a task
  may fail or be delayed because a link could not be built.
"""

from __future__ import annotations

import asyncio
import logging
import os
import typing

import modal

logger = logging.getLogger(__name__)

MODAL_EXECUTOR_NAME = "modal"
"""Executor name recorded with detached executions (see DetachedHandle)."""

STARDAG_BUILD_ID_ENV = "STARDAG_BUILD_ID"
"""Env var through which the build id reaches Modal workers.

Injected into ``env_overrides`` by :class:`ModalTaskExecutor` whenever a
build is active (so it is also set as a process env var around the task's
run). :class:`Runner` reads it to report the task's lifecycle events from
inside the worker, and to bind the structure-scope check to the build (only
``build:<this build>`` is the server's placeholder). Whether the worker
*reports* is a separate switch, ``STARDAG_WORKER_REPORTS_LIFECYCLE``; the
build id is forwarded either way. Riding on ``env_overrides`` keeps the
worker function signature unchanged — older deployed workers simply apply
it as a harmless env var.
"""

STARDAG_WORKER_REPORTS_LIFECYCLE_ENV = "STARDAG_WORKER_REPORTS_LIFECYCLE"
"""Env var switching worker-side lifecycle reporting off (``"0"``).

Set by :class:`ModalTaskExecutor` when it was constructed with
``worker_reports_lifecycle=False`` — a custom or legacy run function that
does not self-report. It used to be inferred from the *absence* of
``STARDAG_BUILD_ID``, which meant a non-reporting worker also lost the
build id its scope check needs; the switch is explicit now so the id can
always travel. Absent means reporting is on.
"""

STARDAG_MODAL_APP_NAME_ENV = "STARDAG_MODAL_APP_NAME"
"""Env var carrying the Modal app name to workers (reactive scheduling).

Lets a worker wake the scheduler by spawning the app's ``tick`` function
when it finishes a task. Transported like ``STARDAG_BUILD_ID``.
"""

STARDAG_REACTIVE_ENV = "STARDAG_REACTIVE"
"""Env var flagging reactive scheduling to workers ("1" when reactive).

In reactive mode the worker additionally registers dynamically yielded
deps — which is what a later tick rebuilds them from — and wakes the
scheduler after terminal events; there is no resident orchestrator to do
either.
"""

STARDAG_EXECUTION_ID_ENV = "STARDAG_EXECUTION_ID"
"""Env var carrying the identity of the execution this container *is*.

Minted by the orchestrator before it claimed the task — the claim is
taken before the spawn, so the executor ref does not exist yet — and
forwarded per call so the worker can name its own execution when it
reports.

Two rules depend on the worker sending it back. Its own TASK_STARTED is
non-claiming and the registry refuses one that names an execution the
task no longer runs under, which is what stops a late restart from
evicting the build that took the task over meanwhile. And its
interruption and preemption reports are honoured only while the task
still holds the execution they name.

Absent (an orchestrator predating it), the worker reports without one and
the registry falls back to the ``(executor, executor_ref)`` pair — the
behaviour of every release before this, so a rolling deploy is safe in
both directions.
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

STARDAG_BUILD_CONFIG_ENV = "STARDAG_BUILD_CONFIG"
"""Env var carrying the build's ``build_config`` (compact JSON) to workers.

A worker installs it before the task runs, so tasks the task constructs —
its dynamic dependencies — resolve their ``dependencies_only`` and
``execution_only`` fields from the same config the bootstrap and the ticks
use. Transported like ``STARDAG_BUILD_ID``; absent means ``{}``.
"""

STARDAG_SCOPE_KEY_ENV = "STARDAG_SCOPE_KEY"
"""Env var carrying the build's structure scope key to workers.

A worker runs whatever task it is handed — the task id promises the output
whatever code produces it — but records the dynamic dependencies it yields
under **its own** code id with the config half of this scope (see
``_runner.worker_scope_key``), so a build that rolled over to a newer
deployment never inherits an old worker's late yield. Absent, or the
server's synthetic ``build:<id>``, means the server's default scope.
"""

STARDAG_MODAL_FUNCTION_TIMEOUT_ENV = "STARDAG_MODAL_FUNCTION_TIMEOUT"
"""Env var carrying the worker function's declared ``timeout``, in seconds.

Read only by ``_runner._classify_interruption``'s **fallback**, and it is
worth knowing why that is all it is now.

It was forwarded to tell a **timeout** apart from a **cancellation**, which
arrive identically: Modal signals SIGUSR1 and the container sees
``modal.exception.InputCancellation("Input was cancelled by user")`` — same
type, same message, whether the function ran out of time or somebody called
``FunctionCall.cancel()``. Verified live (modal 1.5.0). The separator was
*when* the signal lands, Modal delivering the timeout one at the declared
timeout to the millisecond (20.000s / 20.001s / 19.997s across five probes
of a 20s function).

**That distinction turned out not to be the one the worker needs.** What it
needs is whether the backend will restart the input — only a *preemption*
does, and a preemption is a ``KeyboardInterrupt``, so the exception type
answers it exactly. A timeout and a cancel both mean "nothing is coming",
both are reported, and whether a report *applies* is decided by the
registry, which issued any cancel. Elapsed time is consulted only when the
raised exception has no platform signal on its chain at all — a task that
raised ``ResumableInterruption`` on its own initiative.

Absent — an older orchestrator, or a worker function that declares no
``timeout`` of its own — that fallback still errs towards reporting, because
the backend applies its own default timeout regardless: "unknown" does not
mean "no timeout fired", and guessing wrong in that direction strands the
task. See ``_runner._classify_interruption``.
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
