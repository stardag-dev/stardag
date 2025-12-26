"""Configuration commands for Stardag CLI.

Manages registries, profiles, and shows current configuration.
Configuration is stored in ~/.stardag/config.toml.
"""

import typer

from stardag.cli import registry
from stardag.cli.credentials import (
    add_profile,
    ensure_access_token,
    get_access_token,
    get_config_path,
    get_default_profile,
    get_registry_url,
    list_profiles,
    list_registries,
    remove_profile,
    resolve_org_slug_to_id,
    resolve_workspace_slug_to_id,
    set_default_profile,
    set_target_roots,
)
from stardag.config import get_config

app = typer.Typer(help="Manage Stardag CLI configuration")


app.add_typer(registry.app, name="registry")


def _get_authenticated_client(registry: str | None = None, org_id: str | None = None):
    """Get an authenticated HTTP client.

    Returns tuple of (client, api_url, access_token) or raises Exit if not authenticated.
    """
    try:
        import httpx
    except ImportError:
        typer.echo(
            "Error: httpx is required. Install with: pip install stardag[cli]", err=True
        )
        raise typer.Exit(1)

    config = get_config()
    registry_name = registry or config.context.registry_name
    organization_id = org_id or config.context.organization_id

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
    access_token = get_access_token(registry_name, organization_id)
    if not access_token:
        typer.echo(
            "Error: No access token. Run 'stardag auth refresh' or 'stardag auth login'.",
            err=True,
        )
        raise typer.Exit(1)

    headers = {"Authorization": f"Bearer {access_token}"}
    client = httpx.Client(timeout=10.0, headers=headers)

    return client, api_url, access_token


# --- Main config commands ---


@app.command("show")
def show_config() -> None:
    """Show current configuration and active context."""
    config = get_config()

    typer.echo("Configuration:")
    typer.echo(f"  Config file: {get_config_path()}")
    typer.echo("")

    typer.echo("Active Context:")
    if config.context.profile:
        typer.echo(f"  Profile: {config.context.profile}")
    else:
        typer.echo("  Profile: (none - using env vars or defaults)")
    typer.echo(f"  Registry: {config.context.registry_name or '(not set)'}")
    typer.echo(f"  API URL: {config.api.url}")
    typer.echo(f"  Organization: {config.context.organization_id or '(not set)'}")
    typer.echo(f"  Workspace: {config.context.workspace_id or '(not set)'}")

    typer.echo("")
    typer.echo("Target Roots:")
    if config.target.roots:
        for name, uri_prefix in sorted(config.target.roots.items()):
            typer.echo(f"  {name}: {uri_prefix}")
    else:
        typer.echo("  (none)")

    typer.echo("")
    typer.echo("Authentication:")
    if config.api_key:
        typer.echo("  Method: API Key")
    elif config.access_token:
        typer.echo("  Method: JWT")
        typer.echo(f"  Token: {config.access_token[:20]}...")
    else:
        typer.echo("  Method: Not authenticated")


# --- Profile commands ---

profile_app = typer.Typer(help="Manage profiles")
app.add_typer(profile_app, name="profile")


@profile_app.command("add")
def profile_add(
    name: str = typer.Argument(..., help="Profile name"),
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    organization: str = typer.Option(
        ..., "--organization", "-o", help="Organization ID or slug"
    ),
    workspace: str = typer.Option(
        ..., "--workspace", "-w", help="Workspace ID or slug"
    ),
    set_default: bool = typer.Option(
        False, "--default", "-d", help="Set as default profile"
    ),
) -> None:
    """Add or update a profile.

    A profile defines the (registry, organization, workspace) tuple
    for easy switching between different contexts.

    Organization and workspace can be specified by ID (UUID) or slug.
    Slugs will be resolved to IDs automatically if you're authenticated.

    Examples:
        stardag config profile add local-dev -r local -o default -w default
        stardag config profile add prod -r central -o my-org -w production --default
    """
    # Verify registry exists
    registries = list_registries()
    if registry not in registries:
        typer.echo(f"Error: Registry '{registry}' not found.", err=True)
        typer.echo("Add it with: stardag config registry add <name> --url <url>")
        raise typer.Exit(1)

    # Resolve organization slug to ID if needed
    org_id = resolve_org_slug_to_id(registry, organization)
    if org_id is None:
        typer.echo(
            f"Warning: Could not resolve organization '{organization}'. "
            "Using as-is (may need to be a valid UUID).",
            err=True,
        )
        org_id = organization
    elif org_id != organization:
        typer.echo(f"Resolved organization '{organization}' -> {org_id}")

    # Resolve workspace slug to ID if needed
    workspace_id = resolve_workspace_slug_to_id(registry, org_id, workspace)
    if workspace_id is None:
        typer.echo(
            f"Warning: Could not resolve workspace '{workspace}'. "
            "Using as-is (may need to be a valid UUID).",
            err=True,
        )
        workspace_id = workspace
    elif workspace_id != workspace:
        typer.echo(f"Resolved workspace '{workspace}' -> {workspace_id}")

    add_profile(name, registry, org_id, workspace_id)
    typer.echo(f"Profile '{name}' added.")

    if set_default:
        set_default_profile(name)
        typer.echo("Set as default profile.")

        # Auto-refresh access token
        typer.echo("Refreshing access token...")
        token = ensure_access_token(registry, org_id)
        if token:
            typer.echo("Access token refreshed successfully.")
        else:
            typer.echo(
                "Warning: Could not refresh access token. "
                "Run 'stardag auth refresh' to authenticate.",
                err=True,
            )

    typer.echo("")
    typer.echo(f"Use with: STARDAG_PROFILE={name}")


@profile_app.command("list")
def profile_list() -> None:
    """List all configured profiles."""
    profiles = list_profiles()
    default = get_default_profile()

    if not profiles:
        typer.echo("No profiles configured.")
        typer.echo("")
        typer.echo(
            "Create one with: stardag config profile add <name> -r <registry> -o <org> -w <workspace>"
        )
        typer.echo("Or run: stardag auth login")
        return

    typer.echo("Profiles:")
    typer.echo("")
    for name, details in profiles.items():
        is_default = " (default)" if name == default else ""
        typer.echo(f"  {name}{is_default}")
        typer.echo(f"    registry: {details['registry']}")
        typer.echo(f"    organization: {details['organization']}")
        typer.echo(f"    workspace: {details['workspace']}")
        typer.echo("")


@profile_app.command("remove")
def profile_remove(
    name: str = typer.Argument(..., help="Profile name to remove"),
) -> None:
    """Remove a profile from configuration."""
    if remove_profile(name):
        typer.echo(f"Profile '{name}' removed.")
    else:
        typer.echo(f"Profile '{name}' not found.")
        raise typer.Exit(1)


@profile_app.command("use")
def profile_use(
    name: str = typer.Argument(..., help="Profile name to set as default"),
) -> None:
    """Set a profile as the default.

    The default profile is used when STARDAG_PROFILE is not set.
    This command also attempts to refresh the access token for the new profile.
    """
    profiles = list_profiles()
    if name not in profiles:
        typer.echo(f"Error: Profile '{name}' not found.", err=True)
        raise typer.Exit(1)

    set_default_profile(name)
    typer.echo(f"Default profile set to: {name}")

    # Auto-refresh access token for the new profile
    profile = profiles[name]
    registry = profile["registry"]
    org_id = profile["organization"]

    typer.echo("Refreshing access token...")
    token = ensure_access_token(registry, org_id)
    if token:
        typer.echo("Access token refreshed successfully.")
    else:
        typer.echo(
            "Warning: Could not refresh access token. "
            "Run 'stardag auth refresh' to authenticate.",
            err=True,
        )


# --- Target roots commands ---

target_roots_app = typer.Typer(help="Manage target roots")
app.add_typer(target_roots_app, name="target-roots")


@target_roots_app.command("list")
def target_roots_list() -> None:
    """List cached target roots for the active context."""
    config = get_config()
    target_roots = config.target.roots

    if not target_roots:
        typer.echo("No target roots cached.")
        typer.echo("")
        typer.echo("Run 'stardag config target-roots sync' to fetch from server.")
        return

    typer.echo("Target Roots:")
    for name, uri_prefix in sorted(target_roots.items()):
        typer.echo(f"  {name}: {uri_prefix}")


@target_roots_app.command("sync")
def target_roots_sync() -> None:
    """Sync target roots from the API.

    Fetches the latest target roots configuration from the central API
    for the active workspace.
    """
    config = get_config()
    org_id = config.context.organization_id
    workspace_id = config.context.workspace_id

    if not org_id:
        typer.echo(
            "Error: No organization set. Use a profile or set STARDAG_ORGANIZATION_ID.",
            err=True,
        )
        raise typer.Exit(1)

    if not workspace_id:
        typer.echo(
            "Error: No workspace set. Use a profile or set STARDAG_WORKSPACE_ID.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url, _ = _get_authenticated_client()

    try:
        typer.echo(f"Syncing target roots from {api_url}...")

        response = client.get(
            f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/target-roots"
        )

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch target roots: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        roots = response.json()
        target_roots = {root["name"]: root["uri_prefix"] for root in roots}

        set_target_roots(
            target_roots,
            registry_url=api_url,
            organization_id=org_id,
            workspace_id=workspace_id,
        )

        if target_roots:
            typer.echo(f"Synced {len(target_roots)} target root(s):")
            for name, uri_prefix in sorted(target_roots.items()):
                typer.echo(f"  {name}: {uri_prefix}")
        else:
            typer.echo("No target roots configured for this workspace.")

    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


# --- List commands (legacy, kept for convenience) ---

list_app = typer.Typer(help="List available resources")
app.add_typer(list_app, name="list")


@list_app.command("organizations")
def list_organizations() -> None:
    """List organizations you have access to."""
    client, api_url, _ = _get_authenticated_client()

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
        return

    config = get_config()
    current_org = config.context.organization_id

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
    config = get_config()
    org_id = config.context.organization_id

    if not org_id:
        typer.echo(
            "Error: No organization set. Use a profile or set STARDAG_ORGANIZATION_ID.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url, _ = _get_authenticated_client()

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

    current_ws = config.context.workspace_id

    typer.echo(f"Workspaces in organization {org_id}:")
    for ws in workspaces:
        marker = " *" if ws["id"] == current_ws else ""
        personal = " (personal)" if ws.get("owner_id") else ""
        typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']}){personal}{marker}")

    if current_ws:
        typer.echo("")
        typer.echo("* = active workspace")
