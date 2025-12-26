"""Registry management commands for Stardag CLI.

Registries represent connections to Stardag API backends.
Configuration is stored in ~/.stardag/config.toml.
"""

import typer

from stardag.cli.credentials import (
    add_registry,
    get_config_path,
    list_registries,
    remove_registry,
)

app = typer.Typer(help="Manage Stardag registries (API backends)")


@app.command("add")
def registry_add(
    name: str = typer.Argument(..., help="Registry name (e.g., 'local', 'central')"),
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Registry API URL (e.g., 'http://localhost:8000')",
    ),
) -> None:
    """Add or update a registry.

    Examples:
        stardag registry add local --url http://localhost:8000
        stardag registry add central --url https://api.stardag.com
    """
    if not url:
        url = typer.prompt("Registry URL")

    add_registry(name, url)
    typer.echo(f"Registry '{name}' added with URL: {url}")
    typer.echo(f"Config saved to: {get_config_path()}")


@app.command("list")
def registry_list() -> None:
    """List all configured registries."""
    registries = list_registries()

    if not registries:
        typer.echo("No registries configured.")
        typer.echo("")
        typer.echo("Add one with: stardag registry add <name> --url <url>")
        return

    typer.echo("Configured registries:")
    typer.echo("")
    for name, url in registries.items():
        typer.echo(f"  {name}: {url}")


@app.command("remove")
def registry_remove(
    name: str = typer.Argument(..., help="Registry name to remove"),
) -> None:
    """Remove a registry from configuration.

    Note: This only removes the registry from config.toml.
    Credentials for this registry are preserved in ~/.stardag/credentials/.
    """
    if remove_registry(name):
        typer.echo(f"Registry '{name}' removed.")
    else:
        typer.echo(f"Registry '{name}' not found.")
        raise typer.Exit(1)
