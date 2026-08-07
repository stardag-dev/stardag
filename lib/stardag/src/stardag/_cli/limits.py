"""Named concurrency-limit management commands for the Stardag CLI.

Named concurrency limits are configured *per environment in the
registry*. The SDK only tags tasks with limit keys (see the Modal how-to
guide); the cap itself lives server-side and is enforced atomically when a
task starts, across all builds in the environment.

These commands wrap the registry's ``/api/v1/concurrency-limits`` endpoints
for the active stardag profile / environment (override with
``-p/--stardag-profile`` and ``-e/--stardag-env``). They can also be
managed in the registry UI: workspace admin -> Concurrency Limits.

    stardag concurrency-limits list [--holders]
    stardag concurrency-limits set <key> <max_concurrent>
    stardag concurrency-limits delete <key> [--yes]
    stardag concurrency-limits holders <key> [--limit N]
    stardag concurrency-limits evict <key> <task_id> [--yes]
"""

from typing import Optional

import typer
from rich.table import Table

# Shared by every registry-backed CLI group; imported into this module's
# namespace so ``stardag._cli.limits._resolve_registry`` stays the patch
# point tests use.
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError

app = typer.Typer(
    help="Manage named concurrency limits for an environment",
    no_args_is_help=True,
)


@app.command("list")
def limits_list(
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    holders: bool = typer.Option(
        False,
        "--holders/--no-holders",
        help="Also fetch each key's current holder count (one extra call per key).",
    ),
) -> None:
    """List the environment's named concurrency limits."""
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        limits = registry.concurrency_limit_list()
        holder_counts: dict[str, int] = {}
        if holders:
            for limit in limits:
                data = registry.concurrency_limit_holders(limit["key"], limit=1)
                holder_counts[limit["key"]] = data.get("total", 0)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

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
    if holders:
        table.add_column("Holders", justify="right")
    for limit in limits:
        row = [limit["key"], str(limit["max_concurrent"])]
        if holders:
            row.append(str(holder_counts.get(limit["key"], 0)))
        table.add_row(*row)
    console.print(table)


@app.command("set")
def limits_set(
    key: str = typer.Argument(..., help="Concurrency-limit key"),
    max_concurrent: int = typer.Argument(
        ..., help="Maximum tasks that may run concurrently for this key (>= 1)"
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
) -> None:
    """Create or update a named concurrency limit (upsert)."""
    if max_concurrent < 1:
        error_console.print("[bold red]Error:[/bold red] max_concurrent must be >= 1")
        raise typer.Exit(1)

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.concurrency_limit_set(key, max_concurrent)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(
        f"[green]Set concurrency limit[/green] "
        f"{result['key']} -> max_concurrent={result['max_concurrent']}"
    )


@app.command("delete")
def limits_delete(
    key: str = typer.Argument(..., help="Concurrency-limit key to delete"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Delete a named concurrency limit (the key becomes unlimited)."""
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
) -> None:
    """List the RUNNING tasks currently holding slots of a key.

    Holders are shown oldest-running first (eviction candidates on top).
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        data = registry.concurrency_limit_holders(key, limit=limit)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    holders = data.get("holders", [])
    total = data.get("total", len(holders))
    if not holders:
        console.print(f"No current holders for concurrency limit '{key}'.")
        return

    table = Table(title=f"Holders of '{key}' (total: {total})")
    table.add_column("Task ID")
    table.add_column("Task")
    table.add_column("Running since")
    table.add_column("Executor")
    for h in holders:
        name = h.get("task_name") or ""
        namespace = h.get("task_namespace") or ""
        qualified = f"{namespace}.{name}" if namespace else name
        table.add_row(
            h.get("task_id", ""),
            qualified,
            h.get("latest_status_at") or "-",
            h.get("latest_executor") or "-",
        )
    console.print(table)
    if total > len(holders):
        console.print(
            f"[dim]Showing {len(holders)} of {total} holders "
            f"(raise --limit to see more).[/dim]"
        )


@app.command("evict")
def limits_evict(
    key: str = typer.Argument(..., help="Concurrency-limit key"),
    task_id: str = typer.Argument(..., help="Task ID of the holder to evict"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Evict a RUNNING slot holder (records TASK_FAILED, freeing its slots).

    Only evict holders whose process you know is dead: the server cannot
    verify liveness, so evicting a task whose worker is still running leaves
    the cap oversubscribed until that worker finishes.
    """
    if not yes:
        typer.confirm(
            f"Evict task '{task_id}' from concurrency limit '{key}'? "
            "This records TASK_FAILED for it.",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.concurrency_limit_evict(key, task_id)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(
        f"[green]Evicted[/green] {result.get('task_id', task_id)} "
        f"(status: {result.get('status', 'unknown')})"
    )
