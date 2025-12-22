"""Configuration commands for Stardag CLI.

Manages the active context (organization and workspace) for SDK operations.
"""

import typer

from stardag.cli.credentials import (
    get_config_path,
    get_organization_id,
    get_workspace_id,
    load_config,
    load_credentials,
    set_api_url,
    set_organization_id,
    set_timeout,
    set_workspace_id,
)

app = typer.Typer(help="Manage Stardag CLI configuration")

# Subcommands for 'set' and 'list'
set_app = typer.Typer(help="Set configuration values")
list_app = typer.Typer(help="List available resources")

app.add_typer(set_app, name="set")
app.add_typer(list_app, name="list")


def _get_authenticated_client():
    """Get an authenticated HTTP client.

    Returns tuple of (client, api_url) or raises Exit if not authenticated.
    """
    try:
        import httpx
    except ImportError:
        typer.echo(
            "Error: httpx is required. Install with: pip install stardag[cli]", err=True
        )
        raise typer.Exit(1)

    creds = load_credentials()
    if creds is None:
        typer.echo("Error: Not logged in. Run 'stardag auth login' first.", err=True)
        raise typer.Exit(1)

    access_token = creds.get("access_token")
    if not access_token:
        typer.echo(
            "Error: Invalid credentials. Run 'stardag auth login' again.", err=True
        )
        raise typer.Exit(1)

    # Get api_url from config (not credentials)
    config = load_config()
    api_url = config.get("api_url")
    if not api_url:
        typer.echo(
            "Error: API URL not configured. Run 'stardag auth login' or 'stardag config set api-url <url>'.",
            err=True,
        )
        raise typer.Exit(1)

    headers = {"Authorization": f"Bearer {access_token}"}
    client = httpx.Client(timeout=10.0, headers=headers)

    return client, api_url


# --- Main config commands ---


@app.command("get")
def get_config() -> None:
    """Show current configuration."""
    config = load_config()

    typer.echo("Current Configuration:")
    typer.echo(
        f"  API URL:      {config.get('api_url', 'not set (default: http://localhost:8000)')}"
    )
    typer.echo(f"  Timeout:      {config.get('timeout', 'not set (default: 30.0)')}")
    typer.echo(f"  Organization: {config.get('organization_id', 'not set')}")
    typer.echo(f"  Workspace:    {config.get('workspace_id', 'not set')}")
    typer.echo("")
    typer.echo(f"Config file: {get_config_path()}")


# --- Set commands ---


@set_app.command("api-url")
def set_api_url_cmd(
    url: str = typer.Argument(..., help="API URL to set"),
) -> None:
    """Set the API URL."""
    set_api_url(url)
    typer.echo(f"API URL set to: {url.rstrip('/')}")


@set_app.command("timeout")
def set_timeout_cmd(
    timeout: float = typer.Argument(..., help="Timeout in seconds"),
) -> None:
    """Set the request timeout."""
    set_timeout(timeout)
    typer.echo(f"Timeout set to: {timeout}s")


@set_app.command("organization")
def set_organization(
    org_id: str = typer.Argument(..., help="Organization ID to set as active"),
) -> None:
    """Set the active organization.

    This also clears the active workspace (since workspaces belong to organizations).
    """
    # Validate the organization exists and user has access
    client, api_url = _get_authenticated_client()

    try:
        # Try to get workspaces for this org (validates access)
        response = client.get(f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces")

        if response.status_code == 404:
            typer.echo(f"Error: Organization '{org_id}' not found.", err=True)
            raise typer.Exit(1)
        elif response.status_code == 403:
            typer.echo(
                f"Error: You don't have access to organization '{org_id}'.", err=True
            )
            raise typer.Exit(1)
        elif response.status_code != 200:
            typer.echo(
                f"Error: Failed to verify organization: {response.status_code}",
                err=True,
            )
            raise typer.Exit(1)

        workspaces = response.json()
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    set_organization_id(org_id)
    typer.echo(f"Active organization set to: {org_id}")

    if workspaces:
        typer.echo("")
        typer.echo("Available workspaces:")
        for ws in workspaces:
            typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']})")
        typer.echo("")
        typer.echo(
            "Set active workspace with: stardag config set workspace <workspace-id>"
        )


@set_app.command("workspace")
def set_workspace(
    workspace_id: str = typer.Argument(..., help="Workspace ID to set as active"),
) -> None:
    """Set the active workspace."""
    # Check if organization is set
    org_id = get_organization_id()
    if not org_id:
        typer.echo(
            "Error: No organization set. Run 'stardag config set organization <id>' first.",
            err=True,
        )
        raise typer.Exit(1)

    # Validate the workspace exists and belongs to the active org
    client, api_url = _get_authenticated_client()

    try:
        response = client.get(f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces")

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch workspaces: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        workspaces = response.json()
        workspace_ids = {ws["id"] for ws in workspaces}

        if workspace_id not in workspace_ids:
            typer.echo(
                f"Error: Workspace '{workspace_id}' not found in organization.",
                err=True,
            )
            typer.echo("")
            typer.echo("Available workspaces:")
            for ws in workspaces:
                typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']})")
            raise typer.Exit(1)

    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    set_workspace_id(workspace_id)
    typer.echo(f"Active workspace set to: {workspace_id}")


# --- List commands ---


@list_app.command("organizations")
def list_organizations() -> None:
    """List organizations you have access to."""
    client, api_url = _get_authenticated_client()

    try:
        response = client.get(f"{api_url}/api/v1/ui/me")

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch profile: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        data = response.json()
        organizations = data.get("organizations", [])
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not organizations:
        typer.echo("No organizations found.")
        typer.echo("")
        typer.echo("Create an organization in the web UI first.")
        return

    current_org = get_organization_id()

    typer.echo("Organizations:")
    for org in organizations:
        marker = " *" if org["id"] == current_org else ""
        typer.echo(
            f"  {org['id']}  {org['name']} ({org['slug']}) [{org['role']}]{marker}"
        )

    if current_org:
        typer.echo("")
        typer.echo("* = active organization")


@list_app.command("workspaces")
def list_workspaces() -> None:
    """List workspaces in the active organization."""
    org_id = get_organization_id()

    if not org_id:
        typer.echo(
            "Error: No organization set. Run 'stardag config set organization <id>' first.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url = _get_authenticated_client()

    try:
        response = client.get(f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces")

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch workspaces: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        workspaces = response.json()
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not workspaces:
        typer.echo(f"No workspaces found in organization {org_id}.")
        return

    current_ws = get_workspace_id()

    typer.echo(f"Workspaces in organization {org_id}:")
    for ws in workspaces:
        marker = " *" if ws["id"] == current_ws else ""
        typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']}){marker}")

    if current_ws:
        typer.echo("")
        typer.echo("* = active workspace")
