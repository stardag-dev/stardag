"""Waking reactive builds from the CLI: ``stardag builds tick``, and the
wake-up a CLI write sends after it lands.

A reactive build progresses only while one of its ticks runs, and the
registry, which sees every write, flags the builds a write is news for but
never spawns. The registry does not flag the build a write came *through*
(its own tick is assumed to be the one writing), so ``stardag tasks retry T
--build B`` used to leave B pending with no tick and nothing to start one
but a watchdog.

So after a write, the CLI flags the build it wrote through and spawns its
tick unless a scheduler holds the lease. Only that build, and only on the
Modal deployment it records: a spawn goes out with this machine's Modal
token, which may belong to another workspace, or default to another
environment, than the one the build runs in. So the CLI spawns only when
the build's recorded Modal workspace is the one this machine's token
resolves to, and looks the app up in the build's recorded environment.
Otherwise the build is flagged without a hand-out stamp, so the next drain
from any tick, worker or watchdog serves it, and the command prints the
manual fallback. The other builds a write may have unblocked are flagged by
the registry and drained by the next tick anywhere; ``stardag builds tick
--flagged`` drains them on request.

Best-effort throughout, the way a tick's drain is: a write that landed never
fails because the wake-up after it did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence
from uuid import UUID

from rich.console import Console
from rich.markup import escape

from stardag.build._wakeups import SpawnTick
from stardag.registry import BuildInfo

logger = logging.getLogger(__name__)

_NO_MODAL = "the modal extra is not installed, or no Modal token is configured"


class Spawner(Protocol):
    def for_build(self, build: BuildInfo) -> tuple[SpawnTick | None, str | None]:
        """The spawn to use for ``build``, or None and why not."""
        ...


class _ModalSpawner:
    """Spawns a build's tick on the Modal deployment the build records."""

    def __init__(self) -> None:
        self._workspace: str | None = None
        self._resolved = False

    def _local_workspace(self) -> str | None:
        if not self._resolved:
            from stardag.integration.modal._metadata import _get_modal_workspace

            try:
                self._workspace = _get_modal_workspace()
            except Exception:
                self._workspace = None
            self._resolved = True
        return self._workspace

    def for_build(self, build: BuildInfo) -> tuple[SpawnTick | None, str | None]:
        from stardag.integration.modal._spawn import spawn_tick

        metadata = build.executor_metadata or {}
        workspace = metadata.get("workspace")
        if not workspace:
            return None, "the build records no Modal workspace to check against"
        local = self._local_workspace()
        if local != workspace:
            return None, (
                f"this machine's Modal token is for workspace {local!r}, and "
                f"the build runs in {workspace!r}"
            )
        environment = metadata.get("environment")

        def spawn(build_id: UUID, app_name: str) -> None:
            spawn_tick(build_id, app_name, environment_name=environment)

        return spawn, None


def modal_spawner() -> Spawner | None:
    """A spawner for this machine's Modal token: the ``modal`` package
    importable and a token (id and secret) configured. Anything going
    wrong on the way is no spawner, never an error: the write it follows
    has landed."""
    try:
        if not has_modal_token():
            return None
        import stardag.integration.modal._spawn  # noqa: F401
    except Exception:
        return None
    return _ModalSpawner()


def has_modal_token() -> bool:
    """A Modal token id *and* secret are configured. Both, as ``selfhost``'s
    Modal check requires: with only an id the spawn fails *after*
    ``notify`` stamped the hand-out, which keeps the build from every other
    drainer for the whole window."""
    try:
        import modal.config

        config = modal.config.config
        return bool(config.get("token_id") and config.get("token_secret"))
    except Exception:
        return False


@dataclass(frozen=True)
class TickRequest:
    """What asking for a tick of one build came to."""

    build_id: UUID
    # "spawned" | "scheduler_live" | "not_running" | "not_reactive" |
    # "no_spawner" | "spawn_failed"
    outcome: str
    app_name: str | None = None
    # Why not, for "no_spawner" and "spawn_failed".
    error: str | None = None


def request_tick(registry, build_id: UUID, spawner: Spawner | None) -> TickRequest:
    """Flag ``build_id`` and spawn its tick unless a scheduler holds the
    lease once the flag is durable (``notify`` reads it after the commit, as
    a worker's wake-up does). ``scheduler_live`` unknown spawns: a redundant
    tick costs a container, a skipped one the build's progress.

    Everything that can refuse the spawn is decided *before* ``notify``,
    because ``can_spawn=True`` stamps a hand-out that hides the build from
    every other drainer for the window. Without a spawn the flag is set
    unstamped, and any drainer can serve it."""
    build = registry.build_get(build_id)
    if build.status != "running":
        return TickRequest(build_id, "not_running")
    app_name = build.reactive_app_name
    if app_name is None:
        return TickRequest(build_id, "not_reactive")
    spawn, refusal = (
        spawner.for_build(build) if spawner is not None else (None, _NO_MODAL)
    )
    notified = registry.build_notify(build_id, can_spawn=spawn is not None)
    if not notified.needs_tick:
        return TickRequest(build_id, "not_running")
    if notified.scheduler_live is True:
        return TickRequest(build_id, "scheduler_live")
    if spawn is None:
        return TickRequest(build_id, "no_spawner", app_name=app_name, error=refusal)
    try:
        spawn(build_id, app_name)
    except Exception as e:
        return TickRequest(build_id, "spawn_failed", app_name=app_name, error=str(e))
    return TickRequest(build_id, "spawned", app_name=app_name)


def drain(registry, spawner: Spawner) -> tuple[list[UUID], list[TickRequest]]:
    """Spawn a tick for every flagged build nobody serves, as a tick does on
    its way out (:func:`stardag.build._wakeups.drain_wake_candidates`, whose
    synchronous twin this is), each on the deployment it records. Returns
    the builds spawned for and the ones that were not. Those were stamped
    by the hand-out, so they are offered again once the window passes."""
    spawned: list[UUID] = []
    failed: list[TickRequest] = []
    for candidate in registry.build_wake_candidates():
        build_id, app_name = candidate.build_id, candidate.reactive_app_name
        try:
            spawn, refusal = spawner.for_build(registry.build_get(build_id))
            if spawn is None:
                failed.append(
                    TickRequest(
                        build_id, "no_spawner", app_name=app_name, error=refusal
                    )
                )
                continue
            spawn(build_id, app_name)
        except Exception as e:
            failed.append(
                TickRequest(build_id, "spawn_failed", app_name=app_name, error=str(e))
            )
            continue
        spawned.append(build_id)
    return spawned, failed


def describe(request: TickRequest) -> str:
    """One line for a person, in rich markup."""
    build = request.build_id
    if request.outcome == "spawned":
        return (
            f"[green]Spawned a scheduler tick[/green] for build {build} on app "
            f"{escape(request.app_name or '')!r}."
        )
    if request.outcome == "scheduler_live":
        return (
            f"Build {build} has a scheduler running; it is flagged and will "
            "act on its next pass."
        )
    if request.outcome == "not_running":
        return f"Build {build} is not running: nothing to tick."
    if request.outcome == "not_reactive":
        return (
            f"Build {build} is not reactively scheduled: the process driving "
            "it picks up changes on its own."
        )
    if request.outcome == "no_spawner":
        return (
            f"[yellow]Build {build} is flagged for a tick, but this machine "
            f"did not start one[/yellow] ({escape(request.error or '')}). The "
            "next tick, worker or watchdog in the environment will serve it; "
            f"to start one now, run `stardag builds tick {build}` with the "
            "Modal token of the build's workspace."
        )
    return (
        f"[yellow]Could not spawn a scheduler tick[/yellow] for build {build} "
        f"on app {escape(request.app_name or '')!r}: {escape(request.error or '')}. "
        f"It stays flagged; re-run `stardag builds tick {build}`."
    )


def wake_after_write(
    registry, build_ids: Sequence[UUID], report: Console
) -> list[TickRequest]:
    """After a CLI write: request a tick for each build the write went
    through. Prints only what a person would act on or want to know (a
    spawn, a failure, the manual fallback); a build that is not running or
    not reactive is left out. Never raises."""
    # Anything, not only a StardagError: the write this follows has landed,
    # and a command must not report it failed because of the wake-up.
    requests: list[TickRequest] = []
    try:
        spawner = modal_spawner()
    except Exception as e:
        logger.warning("No Modal spawner for the wake-up after the write: %s", e)
        spawner = None
    for build_id in build_ids:
        try:
            request = request_tick(registry, build_id, spawner)
            if request.outcome in ("spawned", "no_spawner", "spawn_failed"):
                report.print(describe(request))
        except Exception as e:
            logger.warning("Could not wake build %s after the write: %s", build_id, e)
            continue
        requests.append(request)
    return requests


__all__ = [
    "Spawner",
    "TickRequest",
    "describe",
    "drain",
    "has_modal_token",
    "modal_spawner",
    "request_tick",
    "wake_after_write",
]
