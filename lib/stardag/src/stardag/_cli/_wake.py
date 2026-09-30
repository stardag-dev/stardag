"""Waking reactive builds from the CLI: ``stardag builds tick``, and the
wake-up every CLI write sends after it lands.

A reactive build progresses only while one of its ticks runs, and the
registry, which sees every write, flags the builds a write is news for but
never spawns. Ticks and workers finish the job by draining, and the CLI now
does too: an operator's write is exactly the kind of news nobody else will
act on. The registry does not flag the build a write came *through* (its
own tick is assumed to be the one writing), so ``stardag tasks retry T
--build B`` used to leave B pending with no tick and nothing to start one
but a watchdog.

So after a write, the CLI flags the build it wrote through and spawns its
tick unless a scheduler holds the lease, then drains the environment's other
flagged builds, as a tick does on its way out. Spawning needs the ``modal``
extra and Modal credentials. Without them the build is flagged without a
hand-out stamp, so the next drain from any tick, worker or watchdog serves
it, and the command prints the manual fallback.

Best-effort throughout, the way a tick's drain is: a write that landed never
fails because the wake-up after it did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from rich.console import Console
from rich.markup import escape

from stardag.build._wakeups import SpawnTick

logger = logging.getLogger(__name__)


def modal_spawner() -> SpawnTick | None:
    """The Modal integration's tick spawner, when this machine can use it:
    the ``modal`` package importable and a Modal token (id and secret)
    configured. The
    function is looked up in the ambient Modal environment
    (``MODAL_ENVIRONMENT`` or the profile's default), as ``stardag build
    --app`` does."""
    try:
        from stardag.integration.modal._spawn import spawn_tick
    except ImportError:
        return None
    return spawn_tick if has_modal_token() else None


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
    error: str | None = None


def request_tick(registry, build_id: UUID, spawn: SpawnTick | None) -> TickRequest:
    """Flag ``build_id`` and spawn its tick unless a scheduler holds the
    lease once the flag is durable (``notify`` reads it after the commit, as
    a worker's wake-up does). ``scheduler_live`` unknown spawns: a redundant
    tick costs a container, a skipped one the build's progress.

    Without a spawner the flag is set with ``can_spawn=False``, so it is
    not stamped as handed out, and any drainer can serve it."""
    notified = registry.build_notify(build_id, can_spawn=spawn is not None)
    if not notified.needs_tick:
        return TickRequest(build_id, "not_running")
    if notified.scheduler_live is True:
        return TickRequest(build_id, "scheduler_live")
    app_name = registry.build_get(build_id).reactive_app_name
    if app_name is None:
        return TickRequest(build_id, "not_reactive")
    if spawn is None:
        return TickRequest(build_id, "no_spawner", app_name=app_name)
    try:
        spawn(build_id, app_name)
    except Exception as e:
        return TickRequest(build_id, "spawn_failed", app_name=app_name, error=str(e))
    return TickRequest(build_id, "spawned", app_name=app_name)


def drain(registry, spawn: SpawnTick) -> tuple[list[UUID], list[TickRequest]]:
    """Spawn a tick for every flagged build nobody serves, as a tick does on
    its way out (:func:`stardag.build._wakeups.drain_wake_candidates`, whose
    synchronous twin this is). Returns the builds spawned for and the
    failures. A failed spawn's build was stamped by the hand-out, so it is
    offered again once the hand-out window passes."""
    spawned: list[UUID] = []
    failed: list[TickRequest] = []
    for candidate in registry.build_wake_candidates():
        try:
            spawn(candidate.build_id, candidate.reactive_app_name)
        except Exception as e:
            failed.append(
                TickRequest(
                    candidate.build_id,
                    "spawn_failed",
                    app_name=candidate.reactive_app_name,
                    error=str(e),
                )
            )
            continue
        spawned.append(candidate.build_id)
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
            "cannot start one[/yellow] (the modal extra is not installed, or "
            "no Modal token is configured). The next tick, worker or watchdog "
            f"in the environment will serve it; to start one now, run "
            f"`stardag builds tick {build}` where Modal is configured."
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
    through, then drain the environment's other flagged builds. Prints only
    what a person would act on or want to know (a spawn, a failure, the
    manual fallback); a build that is not running or not reactive is left
    out. Never raises."""
    # Anything, not only a StardagError: the write this follows has landed,
    # and a command must not report it failed because of the wake-up.
    spawn = modal_spawner()
    requests: list[TickRequest] = []
    for build_id in build_ids:
        try:
            request = request_tick(registry, build_id, spawn)
            if request.outcome in ("spawned", "no_spawner", "spawn_failed"):
                report.print(describe(request))
        except Exception as e:
            logger.warning("Could not wake build %s after the write: %s", build_id, e)
            continue
        requests.append(request)
    if spawn is None:
        return requests
    try:
        spawned, failed = drain(registry, spawn)
        if spawned:
            report.print(
                f"Spawned scheduler ticks for {len(spawned)} other flagged "
                "build(s) this write may have unblocked."
            )
        for request in failed:
            report.print(describe(request))
    except Exception as e:
        logger.warning("Could not drain wake candidates after the write: %s", e)
    return requests


__all__ = [
    "TickRequest",
    "describe",
    "drain",
    "has_modal_token",
    "modal_spawner",
    "request_tick",
    "wake_after_write",
]
