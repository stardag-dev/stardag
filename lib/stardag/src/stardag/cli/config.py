"""Configuration commands for Stardag CLI.

Manages the active context (organization and workspace) for SDK operations.
"""

import typer

from stardag.cli.credentials import (
    get_config_path,
    get_organization_id,
    get_target_roots,
    get_workspace_id,
    load_config,
    load_credentials,
    set_api_url,
    set_organization_id,
    set_target_roots,
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

    target_roots = get_target_roots()
    if target_roots:
        typer.echo(f"  Target Roots: {len(target_roots)} configured")
    else:
        typer.echo("  Target Roots: none")

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


def _resolve_organization(client, api_url: str, org_id_or_slug: str) -> str:
    """Resolve an organization ID or slug to an ID.

    Returns the organization ID if found, or the input if it looks like an ID.
    """
    # First check if it's a valid UUID (36 chars with hyphens)
    if len(org_id_or_slug) == 36 and org_id_or_slug.count("-") == 4:
        return org_id_or_slug

    # Try to resolve by slug via /me endpoint
    try:
        response = client.get(f"{api_url}/api/v1/ui/me")
        if response.status_code == 200:
            data = response.json()
            for org in data.get("organizations", []):
                if org["slug"] == org_id_or_slug or org["id"] == org_id_or_slug:
                    return str(org["id"])
    except Exception:
        pass

    # Assume it's an ID if we couldn't resolve
    return org_id_or_slug


@set_app.command("organization")
def set_organization(
    org_id_or_slug: str = typer.Argument(
        ..., help="Organization ID or slug to set as active"
    ),
) -> None:
    """Set the active organization.

    Accepts either an organization ID or slug.
    This also clears the active workspace (since workspaces belong to organizations).
    """
    # Validate the organization exists and user has access
    client, api_url = _get_authenticated_client()

    try:
        # Resolve slug to ID if needed
        org_id = _resolve_organization(client, api_url, org_id_or_slug)

        # Try to get workspaces for this org (validates access)
        response = client.get(f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces")

        if response.status_code == 404:
            typer.echo(f"Error: Organization '{org_id_or_slug}' not found.", err=True)
            raise typer.Exit(1)
        elif response.status_code == 403:
            typer.echo(
                f"Error: You don't have access to organization '{org_id_or_slug}'.",
                err=True,
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
            personal = " (personal)" if ws.get("owner_id") else ""
            typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']}){personal}")
        typer.echo("")
        typer.echo(
            "Set active workspace with: stardag config set workspace <workspace-id-or-slug>"
        )


def _sync_target_roots(client, api_url: str, org_id: str, workspace_id: str) -> bool:
    """Sync target roots from API to local config.

    Returns True if sync was successful, False otherwise.
    """
    try:
        response = client.get(
            f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/target-roots"
        )

        if response.status_code != 200:
            typer.echo(
                f"Warning: Failed to sync target roots: {response.status_code}",
                err=True,
            )
            return False

        roots = response.json()
        target_roots = {root["name"]: root["uri_prefix"] for root in roots}
        set_target_roots(target_roots)
        return True

    except Exception as e:
        typer.echo(f"Warning: Failed to sync target roots: {e}", err=True)
        return False


@set_app.command("workspace")
def set_workspace(
    workspace_id_or_slug: str = typer.Argument(
        ..., help="Workspace ID or slug to set as active"
    ),
) -> None:
    """Set the active workspace.

    Accepts either a workspace ID or slug.
    """
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

        # Resolve slug to ID if needed
        workspace_id = None
        for ws in workspaces:
            if ws["id"] == workspace_id_or_slug or ws["slug"] == workspace_id_or_slug:
                workspace_id = ws["id"]
                break

        if workspace_id is None:
            typer.echo(
                f"Error: Workspace '{workspace_id_or_slug}' not found in organization.",
                err=True,
            )
            typer.echo("")
            typer.echo("Available workspaces:")
            for ws in workspaces:
                personal = " (personal)" if ws.get("owner_id") else ""
                typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']}){personal}")
            raise typer.Exit(1)

        # Save workspace ID and sync target roots
        set_workspace_id(workspace_id)
        typer.echo(f"Active workspace set to: {workspace_id}")

        # Sync target roots from API
        if _sync_target_roots(client, api_url, org_id, workspace_id):
            target_roots = get_target_roots()
            if target_roots:
                typer.echo(f"Synced {len(target_roots)} target root(s)")
            else:
                typer.echo("No target roots configured for this workspace")

    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


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


@list_app.command("target-roots")
def list_target_roots_cmd() -> None:
    """List synced target roots for the active workspace."""
    target_roots = get_target_roots()

    if not target_roots:
        typer.echo("No target roots configured.")
        typer.echo("")
        typer.echo(
            "Set a workspace with 'stardag config set workspace <id>' to sync target roots."
        )
        return

    typer.echo("Target Roots:")
    for name, uri_prefix in sorted(target_roots.items()):
        typer.echo(f"  {name}: {uri_prefix}")


# --- Sync command ---


@app.command("sync")
def sync_config() -> None:
    """Sync workspace settings from the API.

    Fetches the latest target roots configuration from the central API
    for the active workspace.
    """
    org_id = get_organization_id()
    workspace_id = get_workspace_id()

    if not org_id:
        typer.echo(
            "Error: No organization set. Run 'stardag config set organization <id>' first.",
            err=True,
        )
        raise typer.Exit(1)

    if not workspace_id:
        typer.echo(
            "Error: No workspace set. Run 'stardag config set workspace <id>' first.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url = _get_authenticated_client()

    try:
        typer.echo(f"Syncing workspace settings from {api_url}...")

        if _sync_target_roots(client, api_url, org_id, workspace_id):
            target_roots = get_target_roots()
            if target_roots:
                typer.echo(f"Synced {len(target_roots)} target root(s):")
                for name, uri_prefix in sorted(target_roots.items()):
                    typer.echo(f"  {name}: {uri_prefix}")
            else:
                typer.echo("No target roots configured for this workspace.")
        else:
            typer.echo("Warning: Failed to sync target roots.", err=True)
            raise typer.Exit(1)

    finally:
        client.close()
