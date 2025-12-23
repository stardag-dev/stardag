"""Profile management commands for Stardag CLI.

Profiles allow switching between different Stardag API backends
(e.g., local development vs. central/remote deployment).
"""

import typer

from stardag.cli.credentials import (
    create_profile,
    delete_profile,
    get_active_profile,
    list_profiles,
    load_config,
    set_active_profile,
)
from stardag.config import DEFAULT_PROFILE, get_profile_dir

app = typer.Typer(help="Manage Stardag profiles")


@app.command("list")
def list_profiles_cmd() -> None:
    """List all available profiles."""
    profiles = list_profiles()
    active = get_active_profile()

    if not profiles:
        typer.echo("No profiles configured.")
        typer.echo("")
        typer.echo("Create a profile with: stardag profile add <name> --api-url <url>")
        return

    typer.echo("Profiles:")
    for profile in sorted(profiles):
        marker = " *" if profile == active else ""
        config = load_config(profile)
        api_url = config.get("api_url", "not set")
        typer.echo(f"  {profile}: {api_url}{marker}")

    typer.echo("")
    typer.echo("* = active profile")


@app.command("current")
def current_profile() -> None:
    """Show the current active profile."""
    profile = get_active_profile()
    config = load_config(profile)
    api_url = config.get("api_url", "not set")

    typer.echo(f"Active profile: {profile}")
    typer.echo(f"API URL: {api_url}")
    typer.echo(f"Profile directory: {get_profile_dir(profile)}")


@app.command("add")
def add_profile(
    name: str = typer.Argument(..., help="Profile name (e.g., 'local', 'central')"),
    api_url: str = typer.Option(
        ..., "--api-url", "-u", help="API URL for this profile"
    ),
) -> None:
    """Create a new profile with the given API URL."""
    profiles = list_profiles()

    if name in profiles:
        typer.echo(f"Error: Profile '{name}' already exists.", err=True)
        typer.echo(f"Use 'stardag profile use {name}' to switch to it.", err=True)
        raise typer.Exit(1)

    create_profile(name, api_url)
    typer.echo(f"Created profile '{name}' with API URL: {api_url}")
    typer.echo("")
    typer.echo(f"Switch to this profile with: stardag profile use {name}")


@app.command("use")
def use_profile(
    name: str = typer.Argument(..., help="Profile name to switch to"),
) -> None:
    """Switch to a different profile."""
    profiles = list_profiles()

    if name not in profiles:
        typer.echo(f"Error: Profile '{name}' not found.", err=True)
        typer.echo("")
        if profiles:
            typer.echo("Available profiles:")
            for p in sorted(profiles):
                typer.echo(f"  {p}")
        else:
            typer.echo("No profiles exist. Create one with:")
            typer.echo(f"  stardag profile add {name} --api-url <url>")
        raise typer.Exit(1)

    set_active_profile(name)
    config = load_config(name)
    api_url = config.get("api_url", "not set")

    typer.echo(f"Switched to profile '{name}'")
    typer.echo(f"API URL: {api_url}")


@app.command("delete")
def delete_profile_cmd(
    name: str = typer.Argument(..., help="Profile name to delete"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Delete without confirmation"
    ),
) -> None:
    """Delete a profile and all its data."""
    if name == DEFAULT_PROFILE:
        typer.echo(f"Error: Cannot delete the default profile '{name}'.", err=True)
        raise typer.Exit(1)

    active = get_active_profile()
    if name == active:
        typer.echo(f"Error: Cannot delete the active profile '{name}'.", err=True)
        typer.echo(
            "Switch to another profile first: stardag profile use <other-profile>",
            err=True,
        )
        raise typer.Exit(1)

    profiles = list_profiles()
    if name not in profiles:
        typer.echo(f"Error: Profile '{name}' not found.", err=True)
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(
            f"Are you sure you want to delete profile '{name}' and all its data?"
        )
        if not confirm:
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    if delete_profile(name):
        typer.echo(f"Deleted profile '{name}'.")
    else:
        typer.echo(f"Error: Failed to delete profile '{name}'.", err=True)
        raise typer.Exit(1)
