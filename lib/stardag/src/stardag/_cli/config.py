"""Configuration commands for Stardag CLI.

Manages registries, profiles, and shows current configuration.
Configuration is stored in ~/.stardag/config.toml.
"""

import typer

from stardag._cli import registry
from stardag._cli._helpers import get_authenticated_client, validate_active_profile_cli
from stardag._cli.credentials import (
    add_profile,
    ensure_access_token,
    exchange_for_internal_token,
    get_active_profile,
    get_config_path,
    get_default_profile,
    get_environments,
    get_fresh_oidc_token,
    get_registry_url,
    get_user_workspaces,
    list_profiles,
    list_registries,
    remove_profile,
    resolve_environment_slug_to_id,
    resolve_workspace_slug_to_id,
    set_default_profile,
)
from stardag.config import get_config

app = typer.Typer(help="Manage Stardag CLI configuration")


app.add_typer(registry.app, name="registry")


# --- Main config commands ---


@app.command("show")
def show_config() -> None:
    """Show current configuration and active context."""
    validate_active_profile_cli()
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
    typer.echo(f"  API URL: {config.api.url or '(none - local mode)'}")
    # Show slugs alongside IDs if the profile stores a slug (not a raw UUID)
    ws_slug = None
    env_slug = None
    if config.context.profile:
        prof = list_profiles().get(config.context.profile)
        if prof:
            if prof["workspace"] != config.context.workspace_id:
                ws_slug = prof["workspace"]
            if prof["environment"] != config.context.environment_id:
                env_slug = prof["environment"]

    ws_id = config.context.workspace_id or "(not set)"
    env_id = config.context.environment_id or "(not set)"
    ws_display = f"{ws_id} ({ws_slug})" if ws_slug else ws_id
    env_display = f"{env_id} ({env_slug})" if env_slug else env_id
    typer.echo(f"  Workspace: {ws_display}")
    typer.echo(f"  Environment: {env_display}")

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


def _find_user_for_registry(registry: str) -> str | None:
    """Find a user with stored credentials for the given registry.

    Returns the user email if exactly one is found, None otherwise.
    """
    from stardag.config import get_credentials_dir

    creds_dir = get_credentials_dir()
    if not creds_dir.exists():
        return None

    prefix = f"{registry}__"
    users: list[str] = []
    for path in creds_dir.glob(f"{prefix}*.json"):
        safe_user = path.stem[len(prefix) :]
        # Reverse the sanitization from _sanitize_user_for_path
        user = safe_user.replace("_at_", "@")
        users.append(user)

    if len(users) == 1:
        return users[0]
    return None


@profile_app.command("add")
def profile_add(
    name: str = typer.Argument(..., help="Profile name"),
    registry: str | None = typer.Option(None, "--registry", "-r", help="Registry name"),
    user: str | None = typer.Option(None, "--user", "-u", help="User email"),
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", help="Workspace slug (or ID)"
    ),
    environment: str | None = typer.Option(
        None, "--environment", "-e", help="Environment slug (or ID)"
    ),
    set_default: bool = typer.Option(
        False, "--default", "-d", help="Set as default profile"
    ),
) -> None:
    """Add or update a profile.

    A profile defines the (registry, user, workspace, environment) tuple
    for easy switching between different contexts.

    All options are optional — values are inherited from the active profile
    when not specified. If workspace or environment is not provided, you'll
    be prompted to select interactively.

    Examples:
        stardag config profile add prod
        stardag config profile add staging -e staging
        stardag config profile add local-dev -r local -u me@example.com -w my-workspace -e development
        stardag config profile add prod -r cloud -u me@work.com -w my-company -e production --default
    """
    # --- Load active profile defaults ---
    active_profile_defaults: dict[str, str | None] = {
        "registry": None,
        "user": None,
        "workspace": None,
        "environment": None,
    }
    active_name, _ = get_active_profile()
    if active_name:
        profiles = list_profiles()
        if active_name in profiles:
            p = profiles[active_name]
            active_profile_defaults = {
                "registry": p["registry"],
                "user": p.get("user"),
                "workspace": p["workspace"],
                "environment": p["environment"],
            }

    # --- Resolve registry ---
    if not registry:
        registry = active_profile_defaults["registry"]
        if not registry:
            registries = list_registries()
            if len(registries) == 1:
                registry = next(iter(registries))
            elif len(registries) == 0:
                typer.echo(
                    "Error: No registries configured. Run 'stardag auth login' first.",
                    err=True,
                )
                raise typer.Exit(1)
            else:
                typer.echo(
                    "Error: Multiple registries configured. "
                    "Use --registry to specify one.",
                    err=True,
                )
                raise typer.Exit(1)

    # Verify registry exists
    registries = list_registries()
    if registry not in registries:
        typer.echo(f"Error: Registry '{registry}' not found.", err=True)
        typer.echo("Add it with: stardag config registry add <name> --url <url>")
        raise typer.Exit(1)

    registry_url = get_registry_url(registry)
    if not registry_url:
        typer.echo(f"Error: Could not get URL for registry '{registry}'.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Using registry: {registry} ({registry_url})")

    # --- Resolve user ---
    if not user:
        user = active_profile_defaults["user"]
        if not user:
            # Try to find a user with credentials for this registry
            user = _find_user_for_registry(registry)
            if not user:
                typer.echo(
                    "Error: Could not determine user. Use --user to specify.",
                    err=True,
                )
                raise typer.Exit(1)
    typer.echo(f"Using user: {user}")

    # Track OIDC token to avoid redundant refreshes
    oidc_token: str | None = None

    # --- Resolve workspace (interactive if needed) ---
    if not workspace:
        oidc_token = get_fresh_oidc_token(registry, user)
        if not oidc_token:
            typer.echo(
                "Error: Could not get authentication token. "
                "Run 'stardag auth login' first.",
                err=True,
            )
            raise typer.Exit(1)

        workspaces = get_user_workspaces(registry_url, oidc_token)
        if not workspaces:
            typer.echo("Error: No workspaces found.", err=True)
            raise typer.Exit(1)

        if len(workspaces) == 1:
            ws = workspaces[0]
            workspace = ws["slug"]
            typer.echo(f"Using workspace: {workspace}")
        else:
            # Sort with active profile's workspace first
            active_ws = active_profile_defaults["workspace"]
            if active_ws:
                workspaces.sort(key=lambda w: w["slug"] != active_ws)

            typer.echo("")
            typer.echo("Select workspace:")
            for i, ws in enumerate(workspaces):
                typer.echo(f"  {i + 1}. {ws['slug']}")

            choice = typer.prompt("Enter number", type=int, default=1)
            if choice < 1 or choice > len(workspaces):
                typer.echo("Invalid selection")
                raise typer.Exit(1)
            ws = workspaces[choice - 1]
            workspace = ws["slug"]

    # At this point workspace is guaranteed to be set (by CLI arg or interactive selection)
    assert workspace is not None

    # Resolve and cache workspace slug -> ID
    workspace_id = resolve_workspace_slug_to_id(
        registry, workspace, user, oidc_token=oidc_token
    )
    if workspace_id is None:
        typer.echo(
            f"Error: Could not verify workspace '{workspace}'. "
            "Run 'stardag auth login' first or check the slug.",
            err=True,
        )
        raise typer.Exit(1)
    elif workspace_id != workspace:
        typer.echo(f"Verified workspace '{workspace}' (cached ID)")

    # --- Resolve environment (interactive if needed) ---
    if not environment:
        # Get an access token for this workspace
        access_token = ensure_access_token(registry, workspace_id, user)
        if not access_token:
            # Try direct OIDC exchange
            if not oidc_token:
                oidc_token = get_fresh_oidc_token(registry, user)
            if oidc_token:
                try:
                    internal = exchange_for_internal_token(
                        registry_url, oidc_token, workspace_id
                    )
                    access_token = internal["access_token"]
                except Exception:
                    pass

        if not access_token:
            typer.echo(
                "Error: Could not get access token. Run 'stardag auth login' first.",
                err=True,
            )
            raise typer.Exit(1)

        environments = get_environments(registry_url, access_token, workspace_id)
        if not environments:
            typer.echo("Error: No environments found.", err=True)
            raise typer.Exit(1)

        if len(environments) == 1:
            env = environments[0]
            environment = env["slug"]
            typer.echo(f"Using environment: {environment}")
        else:
            # Sort with active profile's environment first
            active_env = active_profile_defaults["environment"]
            if active_env:
                environments.sort(key=lambda e: e["slug"] != active_env)

            typer.echo("")
            typer.echo("Select environment:")
            for i, env in enumerate(environments):
                typer.echo(f"  {i + 1}. {env['slug']}")

            choice = typer.prompt("Enter number", type=int, default=1)
            if choice < 1 or choice > len(environments):
                typer.echo("Invalid selection")
                raise typer.Exit(1)
            env = environments[choice - 1]
            environment = env["slug"]

    # At this point environment is guaranteed to be set
    assert environment is not None

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
    active_profile, active_source = validate_active_profile_cli()

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
    was_default = get_default_profile() == name
    if remove_profile(name):
        typer.echo(f"Profile '{name}' removed.")
        if was_default:
            typer.echo("(Default profile has been unset.)")
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


# --- List commands (legacy, kept for convenience) ---

list_app = typer.Typer(help="List available resources")
app.add_typer(list_app, name="list")


@list_app.command("workspaces")
def list_workspaces() -> None:
    """List workspaces you have access to."""
    client, api_url, _ = get_authenticated_client()

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

    client, api_url, _ = get_authenticated_client()

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
