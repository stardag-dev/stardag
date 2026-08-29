"""The one way this integration starts a scheduler tick.

Shared by the tick's exit hand-off and cross-build drain, the foreign-app
forward, the worker's own wake-up and the hybrid resident build — all of
which mean exactly "somebody has to look at this build, and it is not me".
A leaf module, so both ``_tick`` and ``_executor`` can import it.
"""

from __future__ import annotations

from uuid import UUID

import modal


def spawn_tick(build_id: UUID, app_name: str) -> None:
    """Spawn the deployed ``tick`` function of ``app_name`` for ``build_id``.

    The signature is :data:`stardag.build._wakeups.SpawnTick`.
    """
    modal.Function.from_name(app_name=app_name, name="tick").spawn(
        build_id=str(build_id)
    )
