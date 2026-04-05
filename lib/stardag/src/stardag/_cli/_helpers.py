"""Shared CLI helpers for Stardag CLI commands."""

import httpx
import typer

from stardag._cli.credentials import (
    InvalidProfileError,
    get_access_token,
    get_registry_url,
    validate_active_profile,
)
from stardag.config.loader import get_config


def validate_active_profile_cli() -> tuple[str, str] | tuple[None, None]:
    """Validate active profile and exit with error if invalid.

    Wrapper around validate_active_profile() that handles CLI error output.
    """
    try:
        return validate_active_profile()
    except InvalidProfileError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def get_authenticated_client(
    registry: str | None = None, workspace_id: str | None = None
) -> tuple[httpx.Client, str, str]:
    """Get an authenticated HTTP client.

    Returns tuple of (client, api_url, access_token) or raises Exit if not authenticated.
    """
    # Validate active profile if we're going to use it
    if not registry or not workspace_id:
        validate_active_profile_cli()

    config = get_config()
    registry_name = registry or config.context.registry_name
    ws_id = workspace_id or (config.registry.workspace_id if config.registry else None)

    if not registry_name:
        typer.echo(
            "Error: No registry configured. Set STARDAG_PROFILE or run 'stardag auth login'.",
            err=True,
        )
        raise typer.Exit(1)

    api_url = get_registry_url(registry_name)
    if not api_url:
        typer.echo(f"Error: Registry '{registry_name}' not found.", err=True)
        raise typer.Exit(1)

    # Get access token from cache
    access_token = get_access_token(registry_name, ws_id)
    if not access_token:
        typer.echo(
            "Error: No access token. Run 'stardag auth refresh' or 'stardag auth login'.",
            err=True,
        )
        raise typer.Exit(1)

    headers = {"Authorization": f"Bearer {access_token}"}
    client = httpx.Client(timeout=10.0, headers=headers)

    return client, api_url, access_token
