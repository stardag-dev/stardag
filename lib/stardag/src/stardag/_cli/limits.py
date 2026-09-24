"""``stardag concurrency-limits``: named concurrency limits (registry v2).

A limit caps how many tasks carrying a key may hold a **live claim** at
once in an environment; the claiming start enforces it. The SDK only tags
tasks with limit keys (see the Modal how-to guide) — the cap itself lives
server-side. These commands wrap the registry's
``GET/PUT/DELETE /api/v2/concurrency-limits`` routes for the active
stardag profile/environment (override with ``-p/--stardag-profile`` and
``-e/--stardag-env``).

    stardag concurrency-limits list [--holders]
    stardag concurrency-limits set <key> <max_concurrent>
    stardag concurrency-limits delete <key> [--yes]
    stardag concurrency-limits holders <key> [--limit N]

There is no ``evict``. A v1 slot could be force-released by an admin; a v2
slot is released by ending the execution that holds it — the operator
recovery for a holder whose worker is gone is
``stardag builds stop --mark-lost``, not a limits command.
"""

from typing import Optional

import typer
from rich.table import Table

from stardag._cli._output import JSON_OPTION, YES_OPTION, age, emit_json, short
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import ConcurrencyLimitHolderInfo, ConcurrencyLimitInfo

app = typer.Typer(
    help="Manage named concurrency limits for an environment.",
    no_args_is_help=True,
)


def _render_holders(key: str, holders: list[ConcurrencyLimitHolderInfo]) -> None:
    table = Table(title=f"Holders of '{key}' (oldest running first)")
    for col in ("Task", "Task ID", "Build", "Execution", "Running for"):
        table.add_column(col)
    for h in holders:
        table.add_row(
            h.task_name,
            short(h.task_id),
            short(h.build_id),
            short(h.execution_id),
            age(h.started_at),
        )
    console.print(table)


@app.command("list")
def limits_list(
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    holders: bool = typer.Option(
        False,
        "--holders/--no-holders",
        help="Also list each key's current holders (one table per key).",
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """List the environment's named concurrency limits, with how many
    slots of each are in use.

    Reads ``GET /concurrency-limits``. Writes nothing.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        limits = registry.concurrency_limit_list_detailed(include_holders=holders)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    if json_output:
        emit_json({"limits": [limit.model_dump(mode="json") for limit in limits]})
        return

    if not limits:
        console.print("No concurrency limits configured for this environment.")
        console.print(
            "\n[dim]Add one with: "
            "stardag concurrency-limits set <key> <max_concurrent>[/dim]"
        )
        return

    table = Table(title="Concurrency Limits")
    table.add_column("Key")
    table.add_column("Max concurrent", justify="right")
    table.add_column("In use", justify="right")
    for limit in limits:
        table.add_row(limit.key, str(limit.max_concurrent), str(limit.in_use))
    console.print(table)

    if holders:
        for limit in limits:
            if limit.holders:
                _render_holders(limit.key, limit.holders)


@app.command("set")
def limits_set(
    key: str = typer.Argument(..., help="Concurrency-limit key"),
    max_concurrent: int = typer.Argument(
        ..., help="Maximum tasks that may run concurrently for this key (>= 0)"
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
) -> None:
    """Create or update a named concurrency limit (upsert).

    Writes ``PUT /concurrency-limits/{key}``. ``max_concurrent=0`` blocks
    the key entirely — every claiming start carrying it is refused.
    """
    if max_concurrent < 0:
        error_console.print("[bold red]Error:[/bold red] max_concurrent must be >= 0")
        raise typer.Exit(1)

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        registry.concurrency_limit_set(key, max_concurrent)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(
        f"[green]Set concurrency limit[/green] {key} -> max_concurrent={max_concurrent}"
    )


@app.command("delete")
def limits_delete(
    key: str = typer.Argument(..., help="Concurrency-limit key to delete"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = YES_OPTION,
) -> None:
    """Delete a named concurrency limit (the key becomes unlimited).

    Writes ``DELETE /concurrency-limits/{key}`` (404 ``unknown_limit`` if
    there is none).
    """
    if not yes:
        typer.confirm(
            f"Delete concurrency limit '{key}'? The key will become unlimited.",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        registry.concurrency_limit_delete(key)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(f"[green]Deleted concurrency limit '{key}'.[/green]")


@app.command("holders")
def limits_holders(
    key: str = typer.Argument(..., help="Concurrency-limit key"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    limit: int = typer.Option(
        100, "--limit", "-n", min=1, max=1000, help="Max holders to display."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """List the tasks currently holding slots of a limit key.

    Reads ``GET /concurrency-limits?include_holders=true`` and picks out
    this key. Holders are shown oldest-running first. A key with no
    configured limit is not listed here at all (it is unlimited, and v2
    tracks holders only against a configured key) — configure one first
    with ``stardag concurrency-limits set``.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        limits: list[ConcurrencyLimitInfo] = registry.concurrency_limit_list_detailed(
            include_holders=True
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    match = next((info for info in limits if info.key == key), None)
    holders = list(match.holders or []) if match else []
    total = len(holders)
    shown = holders[:limit]

    if json_output:
        emit_json(
            {
                "key": key,
                "max_concurrent": match.max_concurrent if match else None,
                "total": total,
                "holders": [h.model_dump(mode="json") for h in shown],
            }
        )
        return

    if match is None:
        console.print(
            f"No concurrency limit '{key}' is configured for this environment."
        )
        return
    if not holders:
        console.print(f"No current holders for concurrency limit '{key}'.")
        return

    _render_holders(key, shown)
    if total > len(shown):
        console.print(
            f"[dim]Showing {len(shown)} of {total} holders "
            "(raise --limit to see more).[/dim]"
        )
