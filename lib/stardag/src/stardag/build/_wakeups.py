"""The spawn half of a cross-build wake-up: drain the registry's wake candidates.

The registry flags every reactive build whose frontier a status change may
have touched — it sees every write, so it can do that completely — but it
has no executor and never spawns. Whoever *does* have an executor and is
already talking to the registry finishes the job: it asks for the flagged
builds nobody is serving and spawns one tick each. The server hands each
build out once per window, so however many schedulers ask at once, a
flagged build costs one container.

Two callers share this: the reactive tick (after every pass that acted, and
on its way out) and the resident engine when its executor can reach a
deployed tick (a hybrid run — local driver, Modal workers). Each supplies
its own ``spawn``; this module only knows how to ask and how to fail
quietly.
"""

from __future__ import annotations

import logging
import typing
from uuid import UUID

from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry import RegistryABC

logger = logging.getLogger(__name__)

SpawnTick = typing.Callable[[UUID, str], None]
"""Spawn a scheduler tick for ``build_id`` on the deployed app ``app_name``.

Executor-integration knowledge: ``stardag.build`` is deliberately agnostic
about how a tick is started, and the same callable serves the tick's exit
hand-off and the cross-build drain — both mean exactly "somebody has to look
at this build, and it is not me".
"""

# Set once per process when the registry answers the wake-candidates route
# with a missing-route 404 — an older server, where cross-build wake-ups
# remain the watchdog's job. Process-global, like the tick-summary flag, so
# a process against such a server stops paying for a doomed request on
# every pass.
_wake_candidates_route_missing = False


async def drain_wake_candidates(
    registry: RegistryABC,
    spawn: SpawnTick,
    *,
    build_id: UUID | None = None,
) -> int:
    """Spawn a tick for every flagged, unserved build the registry hands out.

    Returns how many were spawned. Best-effort throughout, and that is a
    contract rather than a shortcut: this runs on the hot path of every
    scheduler pass and on the exit path of every tick, so nothing it can
    raise is worth propagating — a failed drain degrades to the previous
    behaviour (the flag stays set; the next caller or the watchdog picks it
    up), never to a failed pass.

    Each spawn is guarded on its own. A neighbour whose app was deleted or
    renamed must not stop the rest from being woken.

    ``build_id`` is the caller's own build, for the log line only — the
    server does not need it, since which builds need a tick has nothing to
    do with who is asking.
    """
    global _wake_candidates_route_missing
    if _wake_candidates_route_missing:
        return 0
    try:
        candidates = await registry.build_wake_candidates_aio()
    except NotFoundError as e:
        if is_missing_route_error(e):
            _wake_candidates_route_missing = True
            logger.debug(
                "Registry API does not support wake candidates; cross-build "
                "wake-ups are left to the watchdog in this process. Upgrade "
                "stardag-api to have finishing builds wake their neighbours."
            )
        else:
            logger.warning("Wake candidates not fetched (ignored): %s", e)
        return 0
    except Exception as e:
        logger.warning("Wake candidates not fetched (ignored): %s", e)
        return 0

    spawned = 0
    for candidate in candidates:
        try:
            spawn(candidate.build_id, candidate.reactive_app_name)
        except Exception as e:
            logger.warning(
                "Could not spawn a scheduler tick for build %s on app %r "
                "(ignored; it is offered again once the hand-out window "
                "passes): %s",
                candidate.build_id,
                candidate.reactive_app_name,
                e,
            )
            continue
        spawned += 1
        logger.info(
            "Build %s is flagged with no scheduler live; spawned its tick on app %r%s.",
            candidate.build_id,
            candidate.reactive_app_name,
            f" (drained by build {build_id})" if build_id is not None else "",
        )
    return spawned
