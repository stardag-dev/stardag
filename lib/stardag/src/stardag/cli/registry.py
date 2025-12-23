"""Registry management commands for Stardag CLI.

Registries allow switching between different Stardag API backends
(e.g., local development vs. central/remote deployment).
"""

import typer

from stardag.cli.credentials import (
    create_registry,
    delete_registry,
    get_active_registry,
    list_registries,
    load_config,
    set_active_registry,
)
from stardag.config import DEFAULT_REGISTRY, get_registry_dir

app = typer.Typer(help="Manage Stardag registries")


@app.command("list")
def list_registries_cmd() -> None:
    """List all available registries."""
    registries = list_registries()
    active = get_active_registry()

    if not registries:
        typer.echo("No registries configured.")
        typer.echo("")
        typer.echo(
            "Create a registry with: stardag registry add <name> --api-url <url>"
        )
        return

    typer.echo("Registries:")
    for registry in sorted(registries):
        marker = " *" if registry == active else ""
        config = load_config(registry)
        api_url = config.get("api_url", "not set")
        typer.echo(f"  {registry}: {api_url}{marker}")

    typer.echo("")
    typer.echo("* = active registry")


@app.command("current")
def current_registry() -> None:
    """Show the current active registry."""
    registry = get_active_registry()
    config = load_config(registry)
    api_url = config.get("api_url", "not set")

    typer.echo(f"Active registry: {registry}")
    typer.echo(f"API URL: {api_url}")
    typer.echo(f"Registry directory: {get_registry_dir(registry)}")


@app.command("add")
def add_registry(
    name: str = typer.Argument(..., help="Registry name (e.g., 'local', 'central')"),
    api_url: str = typer.Option(
        ..., "--api-url", "-u", help="API URL for this registry"
    ),
) -> None:
    """Create a new registry with the given API URL."""
    registries = list_registries()

    if name in registries:
        typer.echo(f"Error: Registry '{name}' already exists.", err=True)
        typer.echo(f"Use 'stardag registry use {name}' to switch to it.", err=True)
        raise typer.Exit(1)

    create_registry(name, api_url)
    typer.echo(f"Created registry '{name}' with API URL: {api_url}")
    typer.echo("")
    typer.echo(f"Switch to this registry with: stardag registry use {name}")


@app.command("use")
def use_registry(
    name: str = typer.Argument(..., help="Registry name to switch to"),
) -> None:
    """Switch to a different registry."""
    registries = list_registries()

    if name not in registries:
        typer.echo(f"Error: Registry '{name}' not found.", err=True)
        typer.echo("")
        if registries:
            typer.echo("Available registries:")
            for r in sorted(registries):
                typer.echo(f"  {r}")
        else:
            typer.echo("No registries exist. Create one with:")
            typer.echo(f"  stardag registry add {name} --api-url <url>")
        raise typer.Exit(1)

    set_active_registry(name)
    config = load_config(name)
    api_url = config.get("api_url", "not set")

    typer.echo(f"Switched to registry '{name}'")
    typer.echo(f"API URL: {api_url}")


@app.command("delete")
def delete_registry_cmd(
    name: str = typer.Argument(..., help="Registry name to delete"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Delete without confirmation"
    ),
) -> None:
    """Delete a registry and all its data."""
    if name == DEFAULT_REGISTRY:
        typer.echo(f"Error: Cannot delete the default registry '{name}'.", err=True)
        raise typer.Exit(1)

    active = get_active_registry()
    if name == active:
        typer.echo(f"Error: Cannot delete the active registry '{name}'.", err=True)
        typer.echo(
            "Switch to another registry first: stardag registry use <other-registry>",
            err=True,
        )
        raise typer.Exit(1)

    registries = list_registries()
    if name not in registries:
        typer.echo(f"Error: Registry '{name}' not found.", err=True)
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(
            f"Are you sure you want to delete registry '{name}' and all its data?"
        )
        if not confirm:
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    if delete_registry(name):
        typer.echo(f"Deleted registry '{name}'.")
    else:
        typer.echo(f"Error: Failed to delete registry '{name}'.", err=True)
        raise typer.Exit(1)
