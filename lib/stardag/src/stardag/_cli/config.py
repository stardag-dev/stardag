"""Configuration commands for Stardag CLI.

Manages registries, profiles, and shows current configuration.
Configuration is stored in ~/.stardag/config.toml.
"""

import typer

from stardag._cli import registry
from stardag._cli.credentials import (
    InvalidProfileError,
    add_profile,
    ensure_access_token,
    get_access_token,
    get_config_path,
    get_registry_url,
    list_profiles,
    list_registries,
    remove_profile,
    resolve_environment_slug_to_id,
    resolve_workspace_slug_to_id,
    set_default_profile,
    set_target_roots,
    validate_active_profile,
)
from stardag.config import get_config

app = typer.Typer(help="Manage Stardag CLI configuration")


app.add_typer(registry.app, name="registry")


def _validate_active_profile_cli() -> tuple[str, str] | tuple[None, None]:
    """Validate active profile and exit with error if invalid.

    Wrapper around validate_active_profile() that handles CLI error output.
    """
    try:
        return validate_active_profile()
    except InvalidProfileError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


def _get_authenticated_client(
    registry: str | None = None, workspace_id: str | None = None
):
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

    # Validate active profile if we're going to use it
    if not registry or not workspace_id:
        _validate_active_profile_cli()

    config = get_config()
    registry_name = registry or config.context.registry_name
    ws_id = workspace_id or config.context.workspace_id

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


# --- Main config commands ---


@app.command("show")
def show_config() -> None:
    """Show current configuration and active context."""
    _validate_active_profile_cli()
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
    typer.echo(f"  Workspace: {config.context.workspace_id or '(not set)'}")
    typer.echo(f"  Environment: {config.context.environment_id or '(not set)'}")

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
    user: str = typer.Option(..., "--user", "-u", help="User email"),
    workspace: str = typer.Option(
        ..., "--workspace", "-w", help="Workspace slug (or ID)"
    ),
    environment: str = typer.Option(
        ..., "--environment", "-e", help="Environment slug (or ID)"
    ),
    set_default: bool = typer.Option(
        False, "--default", "-d", help="Set as default profile"
    ),
) -> None:
    """Add or update a profile.

    A profile defines the (registry, user, workspace, environment) tuple
    for easy switching between different contexts.

    Workspace and environment should be specified by slug for readability.
    The IDs are resolved and cached automatically when authenticated.

    Examples:
        stardag config profile add local-dev -r local -u me@example.com -w my-workspace -e development
        stardag config profile add prod -r cloud -u me@work.com -w my-company -e production --default
    """
    # Verify registry exists
    registries = list_registries()
    if registry not in registries:
        typer.echo(f"Error: Registry '{registry}' not found.", err=True)
        typer.echo("Add it with: stardag config registry add <name> --url <url>")
        raise typer.Exit(1)

    # Resolve and cache workspace slug -> ID
    # This validates the slug exists and populates the cache
    workspace_id = resolve_workspace_slug_to_id(registry, workspace, user)
    if workspace_id is None:
        typer.echo(
            f"Error: Could not verify workspace '{workspace}'. "
            "Run 'stardag auth login' first or check the slug.",
            err=True,
        )
        raise typer.Exit(1)
    elif workspace_id != workspace:
        typer.echo(f"Verified workspace '{workspace}' (cached ID)")

    # Resolve and cache environment slug -> ID
    environment_id = resolve_environment_slug_to_id(
        registry, workspace_id, environment, user
    )
    if environment_id is None:
        typer.echo(
            f"Error: Could not verify environment '{environment}'. "
            "Run 'stardag auth login' first or check the slug.",
            err=True,
        )
        raise typer.Exit(1)
    elif environment_id != environment:
        typer.echo(f"Verified environment '{environment}' (cached ID)")

    # Store slugs in profile (not IDs) for readability
    add_profile(name, registry, workspace, environment, user)
    typer.echo(f"Profile '{name}' added.")
    typer.echo(f"  User: {user}")

    if set_default:
        set_default_profile(name)
        typer.echo("Set as default profile.")

        # Auto-refresh access token
        typer.echo("Refreshing access token...")
        token = ensure_access_token(registry, workspace_id, user)
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
    active_profile, active_source = _validate_active_profile_cli()

    if not profiles:
        typer.echo("No profiles configured.")
        typer.echo("")
        typer.echo(
            "Create one with: stardag config profile add <name> -r <registry> -w <workspace> -e <environment>"
        )
        typer.echo("Or run: stardag auth login")
        return

    typer.echo("Profiles:")
    typer.echo("")
    for name, details in profiles.items():
        is_active = " *" if name == active_profile else ""
        typer.echo(f"  {name}{is_active}")
        typer.echo(f"    registry: {details['registry']}")
        typer.echo(f"    user: {details.get('user', '(not set)')}")
        typer.echo(f"    workspace: {details['workspace']}")
        typer.echo(f"    environment: {details['environment']}")
        typer.echo("")

    # Show explanation of active profile
    typer.echo("")
    if active_profile and active_source == "env":
        typer.echo(f"* active profile (via STARDAG_PROFILE={active_profile})")
    elif active_profile and active_source == "default":
        typer.echo(f"* active profile (via [default] in {get_config_path()})")
    else:
        typer.echo("No active profile. To set one:")
        typer.echo("  - Set env var: export STARDAG_PROFILE=<profile-name>")
        typer.echo("  - Or set default: stardag config profile use <profile-name>")


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
    user = profile["user"]
    workspace_slug = profile["workspace"]
    environment_slug = profile["environment"]

    if not user:
        typer.echo(
            "Warning: Profile has no user set. "
            "Run 'stardag auth login' to authenticate and update the profile.",
            err=True,
        )
        return

    # Resolve workspace slug to ID (needed for token operations)
    workspace_id = resolve_workspace_slug_to_id(registry, workspace_slug, user)
    if workspace_id is None:
        typer.echo(
            f"Warning: Could not resolve workspace '{workspace_slug}'. "
            "Run 'stardag auth login' to authenticate.",
            err=True,
        )
        return

    # Also resolve environment to populate cache
    resolve_environment_slug_to_id(registry, workspace_id, environment_slug, user)

    typer.echo("Refreshing access token...")
    token = ensure_access_token(registry, workspace_id, user)
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
    _validate_active_profile_cli()
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
    for the active environment.
    """
    _validate_active_profile_cli()
    config = get_config()
    workspace_id = config.context.workspace_id
    environment_id = config.context.environment_id

    if not workspace_id:
        typer.echo(
            "Error: No workspace set. Use a profile or set STARDAG_WORKSPACE_ID.",
            err=True,
        )
        raise typer.Exit(1)

    if not environment_id:
        typer.echo(
            "Error: No environment set. Use a profile or set STARDAG_ENVIRONMENT_ID.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url, _ = _get_authenticated_client()

    try:
        typer.echo(f"Syncing target roots from {api_url}...")

        response = client.get(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots"
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
            workspace_id=workspace_id,
            environment_id=environment_id,
        )

        if target_roots:
            typer.echo(f"Synced {len(target_roots)} target root(s):")
            for name, uri_prefix in sorted(target_roots.items()):
                typer.echo(f"  {name}: {uri_prefix}")
        else:
            typer.echo("No target roots configured for this environment.")

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


@list_app.command("workspaces")
def list_workspaces() -> None:
    """List workspaces you have access to."""
    client, api_url, _ = _get_authenticated_client()

    try:
        response = client.get(f"{api_url}/api/v1/ui/me")

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch profile: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        data = response.json()
        workspaces = data.get("workspaces", [])
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not workspaces:
        typer.echo("No workspaces found.")
        return

    config = get_config()
    current_workspace = config.context.workspace_id

    typer.echo("Workspaces:")
    for ws in workspaces:
        marker = " *" if ws["id"] == current_workspace else ""
        typer.echo(f"  {ws['id']}  {ws['name']} ({ws['slug']}) [{ws['role']}]{marker}")

    if current_workspace:
        typer.echo("")
        typer.echo("* = active workspace")


@list_app.command("environments")
def list_environments() -> None:
    """List environments in the active workspace."""
    config = get_config()
    workspace_id = config.context.workspace_id

    if not workspace_id:
        typer.echo(
            "Error: No workspace set. Use a profile or set STARDAG_WORKSPACE_ID.",
            err=True,
        )
        raise typer.Exit(1)

    client, api_url, _ = _get_authenticated_client()

    try:
        response = client.get(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments"
        )

        if response.status_code != 200:
            typer.echo(
                f"Error: Failed to fetch environments: {response.status_code}", err=True
            )
            raise typer.Exit(1)

        environments = response.json()
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not environments:
        typer.echo(f"No environments found in workspace {workspace_id}.")
        return

    current_env = config.context.environment_id

    typer.echo(f"Environments in workspace {workspace_id}:")
    for env in environments:
        marker = " *" if env["id"] == current_env else ""
        personal = " (personal)" if env.get("owner_id") else ""
        typer.echo(f"  {env['id']}  {env['name']} ({env['slug']}){personal}{marker}")

    if current_env:
        typer.echo("")
        typer.echo("* = active environment")
