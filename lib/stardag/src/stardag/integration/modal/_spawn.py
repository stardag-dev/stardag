"""The one way this integration starts a scheduler tick.

Shared by the tick's exit hand-off and cross-build drain, the foreign-app
forward, the worker's own wake-up and the hybrid resident build — all of
which mean exactly "somebody has to look at this build, and it is not me".
A leaf module, so both ``_tick`` and ``_executor`` can import it.
"""

from __future__ import annotations

import typing
from uuid import UUID

import modal


def spawn_tick(
    build_id: UUID,
    app_name: str,
    tick_kwargs: dict[str, typing.Any] | None = None,
) -> None:
    """Spawn the deployed ``tick`` function of ``app_name`` for ``build_id``.

    With ``tick_kwargs`` omitted — which is what every wake-up does — the
    tick runs on the build's own stored config, so a wake-up gets the same
    scheduler the build was triggered with.

    ``tick_kwargs`` is for a caller that wants a *different* tick: the
    watchdog sweep asks for one pass and no linger, because it is a safety
    net rather than a wake-up (see ``_run_watchdog_sweep``). Two optional
    arguments deep is deliberate — the default has to be "the build's own
    config", or a caller that forgets is silently reconfiguring somebody
    else's scheduler.

    The two-argument form is :data:`stardag.build._wakeups.SpawnTick`.
    """
    kwargs: dict[str, typing.Any] = {"build_id": str(build_id)}
    if tick_kwargs is not None:
        # Omitted rather than sent as None, so a wake-up's spawn is byte-for
        # byte the call it always was. The deployed ``tick`` defaults the
        # parameter to None either way; this keeps "no overrides" and
        # "overrides that happen to be empty" from looking alike on the
        # wire, and keeps the common path free of a parameter it never uses.
        kwargs["tick_kwargs"] = tick_kwargs
    modal.Function.from_name(app_name=app_name, name="tick").spawn(**kwargs)
