"""``stardag cache``: the local cache of instance bodies.

    stardag cache clear            # the active profile's partition
    stardag cache clear --all      # every partition

Instance bodies read from the registry are cached under
``~/.stardag/registry-cache/<partition>/``, one partition per registry
identity (see :mod:`stardag.registry._instance_cache`). Entries are
immutable copies, so clearing is never needed for correctness; it frees the
space, or forces the next read to go to the registry.
"""

from typing import Optional

import typer

from stardag._cli._registry_ctx import (
    _PROFILE_OPTION,
    _resolve_registry,
    console,
    error_console,
)
from stardag.registry import _instance_cache

app = typer.Typer(
    help="Manage the local cache of instance bodies.", no_args_is_help=True
)


@app.command("clear")
def cache_clear(
    all_partitions: bool = typer.Option(
        False, "--all", help="Clear every partition, not only the active profile's."
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
) -> None:
    """Delete cached instance bodies: the active profile's (registry,
    workspace, environment) partition, or with ``--all`` every partition.
    Makes no registry call.

    No ``-e/--stardag-env``: resolving an environment slug may ask the
    registry, and clearing a local cache must work offline. Select another
    environment through its profile, or use ``--all``."""
    if all_partitions:
        removed = _clear_or_exit(None)
        console.print(
            f"Removed {removed} cached instance bod{'y' if removed == 1 else 'ies'} "
            f"from {_instance_cache.cache_root()}."
        )
        return
    registry = _resolve_registry(stardag_profile, None)
    partition = registry.instance_cache_partition()
    if partition is None:
        error_console.print(
            "[bold red]The active profile has no cache partition[/bold red] "
            "(no environment is known for it). Use --all to clear every "
            "partition."
        )
        raise typer.Exit(1)
    removed = _clear_or_exit(partition)
    console.print(
        f"Removed {removed} cached instance bod{'y' if removed == 1 else 'ies'} "
        f"from {_instance_cache.cache_root() / partition}."
    )


def _clear_or_exit(partition: str | None) -> int:
    """Clear, or report what could not be deleted and exit 1."""
    try:
        return _instance_cache.clear(partition)
    except _instance_cache.CacheClearError as e:
        error_console.print(f"[bold red]Cache clear incomplete:[/bold red] {e}")
        for path, error in e.failed[:10]:
            error_console.print(f"  {path}: {error}")
        raise typer.Exit(1) from None
