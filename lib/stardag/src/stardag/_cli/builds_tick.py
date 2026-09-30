"""``stardag builds tick`` and ``builds stalled``: find reactive builds
nobody is serving, and start their scheduler by hand.

The manual fallback for a reactive build whose wake-up was lost, in an
environment with no watchdog. Split from :mod:`stardag._cli.builds`; the
mechanics are in :mod:`stardag._cli._wake`.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

import typer
from rich.table import Table

# Imported into this module's namespace so ``stardag._cli.builds_tick.
# _resolve_registry`` is the patch point for this command.
from stardag._cli._duration import format_duration, parse_duration
from stardag._cli._output import JSON_OPTION, emit_json, parse_uuid, stamp
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag._cli._wake import Spawner, describe, drain, modal_spawner, request_tick
from stardag.exceptions import StardagError

_NO_SPAWNER = (
    "this machine cannot start a scheduler tick: install the modal extra "
    "(`pip install 'stardag[modal]'`) and configure a Modal token "
    "(`modal token new`)."
)


def builds_tick(
    build_id: Optional[str] = typer.Argument(
        None, help="Build ID. Omit with --flagged."
    ),
    flagged: bool = typer.Option(
        False,
        "--flagged",
        help="Instead of one build: spawn a tick for every flagged build in "
        "the environment that no scheduler is serving (what a tick's drain "
        "does).",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Start a scheduler tick for a reactive build.

    The manual fallback when a build's wake-up was lost and no watchdog is
    deployed: flags the build (``POST /builds/{id}/notify``) and, unless a
    scheduler already holds its lease, spawns its app's deployed ``tick``
    function. A live scheduler is left to act on the flag. Idempotent: a
    tick spawned while another holds the lease exits at once.

    ``--flagged`` asks the registry for every flagged build no scheduler is
    serving (``POST /builds/wake-candidates``) and spawns one tick each.

    Needs the modal extra and a Modal token for the workspace the build
    records. The tick is looked up in the Modal environment the build
    records, not the ambient one; a build from another workspace is
    flagged, not spawned for.
    """
    if (build_id is None) == (not flagged):
        error_console.print(
            "[bold red]Error:[/bold red] pass a build ID, or --flagged, but not both."
        )
        raise typer.Exit(1)
    parsed = parse_uuid(build_id, "build ID") if build_id is not None else None
    spawner = modal_spawner()
    if spawner is None:
        error_console.print(f"[bold red]Error:[/bold red] {_NO_SPAWNER}")
        raise typer.Exit(1)
    if parsed is None:
        _tick_flagged(spawner, stardag_profile, stardag_env, json_output)
    else:
        _tick_one(parsed, spawner, stardag_profile, stardag_env, json_output)


def _tick_one(
    build_id: UUID,
    spawner: Spawner,
    stardag_profile: Optional[str],
    stardag_env: Optional[str],
    json_output: bool,
) -> None:
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        request = request_tick(registry, build_id, spawner)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                "build_id": str(request.build_id),
                "outcome": request.outcome,
                "app_name": request.app_name,
                "error": request.error,
            }
        )
    else:
        console.print(describe(request))
    if request.outcome in ("spawn_failed", "no_spawner"):
        raise typer.Exit(1)


def _tick_flagged(
    spawner: Spawner,
    stardag_profile: Optional[str],
    stardag_env: Optional[str],
    json_output: bool,
) -> None:
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        spawned, failed = drain(registry, spawner)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                "spawned": [str(b) for b in spawned],
                "failed": [
                    {"build_id": str(f.build_id), "error": f.error} for f in failed
                ],
            }
        )
    else:
        if not spawned and not failed:
            console.print("No flagged build is waiting for a scheduler.")
        for spawned_id in spawned:
            console.print(
                f"[green]Spawned a scheduler tick[/green] for build {spawned_id}."
            )
        for failure in failed:
            console.print(describe(failure))
    if failed:
        raise typer.Exit(1)


# The server's bounds, checked here for a usage message rather than a 422.
_MAX_STALL_AGE_SECONDS = 7 * 24 * 3600

_REASONS = {
    "flagged_unserved": "flagged, no scheduler",
    "lease_lapsed": "tick died holding the lease",
}


def builds_stalled(
    older_than: str = typer.Option(
        "5m",
        "--older-than",
        help="How long unserved before a build counts (e.g. 90s, 5m, 2h).",
    ),
    limit: int = typer.Option(200, "--limit", help="At most this many (max 200)."),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List running reactive builds no scheduler has served for a while.

    Reads ``GET /stalled-builds``. Two kinds, oldest first: a build
    **flagged** for a tick with no scheduler holding its lease since, and
    one whose last tick **died holding the lease** (it expired and was never
    released). Either waits for the next drain, a watchdog sweep, or
    ``stardag builds tick <id>``. An empty list is the expected state.
    """
    try:
        seconds = parse_duration(older_than)
    except ValueError as e:
        error_console.print(f"[bold red]Error:[/bold red] --older-than: {e}")
        raise typer.Exit(1)
    if seconds > _MAX_STALL_AGE_SECONDS or not 1 <= limit <= 200:
        error_console.print(
            "[bold red]Error:[/bold red] --older-than is at most 7d, and "
            "--limit between 1 and 200."
        )
        raise typer.Exit(1)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        stalled = registry.build_list_stalled(older_than_seconds=seconds, limit=limit)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(
            {
                "older_than_seconds": seconds,
                "builds": [b.model_dump(mode="json") for b in stalled],
            }
        )
        return
    if not stalled:
        console.print(
            f"No reactive build has gone unserved for {format_duration(seconds)}."
        )
        return
    table = Table(title=f"Unserved for {format_duration(seconds)} or more")
    table.add_column("Build")
    table.add_column("App")
    table.add_column("Why")
    table.add_column("Since")
    for build in stalled:
        table.add_row(
            str(build.build_id),
            build.reactive_app_name,
            _REASONS.get(build.reason, build.reason),
            stamp(build.since),
        )
    console.print(table)
    console.print(
        "[dim]Start one with `stardag builds tick <id>`, or all flagged ones "
        "with `stardag builds tick --flagged`.[/dim]"
    )
