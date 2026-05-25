"""Environment management commands for Stardag CLI.

Manage environments and their target roots via the Stardag Registry API.
"""

import json as json_module

import httpx
import typer

from stardag._cli._helpers import get_authenticated_client
from stardag._cli.credentials import (
    resolve_environment_slug_to_id,
    set_target_roots as set_cached_target_roots,
)
from stardag.config.loader import get_config

app = typer.Typer(help="Manage environments and target roots")


def _get_workspace_id() -> str:
    """Get the active workspace ID or exit."""
    config = get_config()
    workspace_id = config.registry.workspace_id if config.registry else None
    if not workspace_id:
        typer.echo(
            "Error: No workspace set. Use a profile or set STARDAG_WORKSPACE_ID.",
            err=True,
        )
        raise typer.Exit(1)
    return workspace_id


def _get_environment_id(env_override: str | None = None) -> str:
    """Get environment ID, resolving slug if needed, or exit."""
    config = get_config()

    if env_override:
        # Resolve slug to ID if needed
        registry_name = config.context.registry_name
        workspace_id = config.registry.workspace_id if config.registry else None
        if not registry_name or not workspace_id:
            typer.echo(
                "Error: No registry/workspace configured. Set STARDAG_PROFILE.",
                err=True,
            )
            raise typer.Exit(1)
        user = config.registry.auth.user_email if config.registry else None
        env_id = resolve_environment_slug_to_id(
            registry_name, workspace_id, env_override, user
        )
        if not env_id:
            typer.echo(
                f"Error: Could not resolve environment '{env_override}'.", err=True
            )
            raise typer.Exit(1)
        return env_id

    environment_id = config.registry.environment_id if config.registry else None
    if not environment_id:
        typer.echo(
            "Error: No environment set. Use a profile, set STARDAG_ENVIRONMENT_ID, "
            "or pass --env.",
            err=True,
        )
        raise typer.Exit(1)
    return environment_id


def _handle_api_error(response: httpx.Response, resource: str = "resource") -> None:
    """Handle API error responses with user-friendly messages."""
    if response.status_code == 401:
        typer.echo(
            "Error: Authentication failed. Run 'stardag auth refresh' or 'stardag auth login'.",
            err=True,
        )
    elif response.status_code == 403:
        try:
            detail = response.json().get("detail", "Permission denied")
        except ValueError:
            detail = "Permission denied"
        typer.echo(f"Error: {detail}", err=True)
    elif response.status_code == 404:
        typer.echo(f"Error: {resource.capitalize()} not found.", err=True)
    elif response.status_code == 409:
        detail = response.json().get("detail", "Conflict")
        typer.echo(f"Error: {detail}", err=True)
    elif response.status_code == 422:
        detail = response.json().get("detail", "Validation error")
        if isinstance(detail, list):
            msgs = [d.get("msg", str(d)) for d in detail]
            typer.echo(f"Error: Validation failed: {'; '.join(msgs)}", err=True)
        else:
            typer.echo(f"Error: {detail}", err=True)
    else:
        typer.echo(f"Error: API request failed (HTTP {response.status_code})", err=True)


def _parse_target_roots(values: list[str]) -> dict[str, str]:
    """Parse target root key=value pairs.

    Args:
        values: List of "name=uri" strings.

    Returns:
        Dict of name to uri_prefix.

    Raises typer.Exit on invalid format.
    """
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            typer.echo(
                f"Error: Invalid target root format '{item}'. Expected 'name=uri'.",
                err=True,
            )
            raise typer.Exit(1)
        name, uri = item.split("=", 1)
        name = name.strip()
        uri = uri.strip()
        if not name:
            typer.echo("Error: Target root name cannot be empty.", err=True)
            raise typer.Exit(1)
        if not uri:
            typer.echo(
                f"Error: Target root URI cannot be empty for '{name}'.", err=True
            )
            raise typer.Exit(1)
        result[name] = uri
    return result


# --- Environment commands ---


@app.command("list")
def env_list() -> None:
    """List environments in the active workspace."""
    workspace_id = _get_workspace_id()
    client, api_url, _ = get_authenticated_client()

    try:
        response = client.get(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments"
        )

        if response.status_code != 200:
            _handle_api_error(response, "environments")
            raise typer.Exit(1)

        environments = response.json()
    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not environments:
        typer.echo("No environments found.")
        return

    config = get_config()
    current_env = config.registry.environment_id if config.registry else None

    typer.echo("Environments:")
    for env in environments:
        marker = " *" if env["id"] == current_env else ""
        desc = f"  - {env['description']}" if env.get("description") else ""
        typer.echo(f"  {env['slug']:<20} {env['name']}{marker}")
        if desc:
            typer.echo(f"  {'':<20} {desc}")

    if current_env:
        typer.echo("")
        typer.echo("* = active environment")


@app.command("create")
def env_create(
    name: str = typer.Argument(..., help="Environment display name"),
    slug: str = typer.Option(
        None,
        "--slug",
        "-s",
        help="URL-safe slug (auto-generated from name if omitted)",
    ),
    description: str = typer.Option(
        None, "--description", "-d", help="Environment description"
    ),
    target_root: list[str] = typer.Option(
        None,
        "--target-root",
        "-t",
        help="Target root as name=uri (repeatable)",
    ),
) -> None:
    """Create a new environment.

    Examples:
        stardag environment create Production --slug production
        stardag environment create Staging -s staging -d "Staging environment"
        stardag environment create Development -s dev -t default=s3://bucket/dev
        stardag environment create CI -s ci -t default=s3://bucket/ci -t s3=s3://other/ci
    """
    # Auto-generate slug from name if not provided
    if not slug:
        slug = name.lower().replace(" ", "-")
        # Strip non-alphanumeric characters (except hyphens/underscores)
        slug = "".join(c for c in slug if c.isalnum() or c in "-_")
        # Ensure valid slug format
        slug = slug.strip("-_")
        if not slug:
            typer.echo(
                "Error: Could not auto-generate slug from name. Please provide --slug.",
                err=True,
            )
            raise typer.Exit(1)

    workspace_id = _get_workspace_id()
    client, api_url, _ = get_authenticated_client()

    try:
        # Create the environment
        payload: dict = {"name": name, "slug": slug}
        if description:
            payload["description"] = description

        response = client.post(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments",
            json=payload,
        )

        if response.status_code != 201:
            _handle_api_error(response, "environment")
            raise typer.Exit(1)

        env = response.json()
        typer.echo(f"Environment '{env['name']}' created (slug: {env['slug']})")

        # Create target roots if provided
        if target_root:
            target_roots = _parse_target_roots(target_root)
            env_id = env["id"]
            created = 0
            for tr_name, uri in target_roots.items():
                tr_response = client.post(
                    f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{env_id}/target-roots",
                    json={"name": tr_name, "uri_prefix": uri},
                )
                if tr_response.status_code == 201:
                    created += 1
                    typer.echo(f"  Target root '{tr_name}' -> {uri}")
                else:
                    _handle_api_error(tr_response, f"target root '{tr_name}'")

            if created > 0:
                typer.echo(f"Created {created} target root(s).")
                _sync_target_roots_cache(
                    client, api_url, workspace_id, env_id, target_roots
                )

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


@app.command("delete")
def env_delete(
    environment: str = typer.Argument(..., help="Environment slug or ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Delete an environment.

    Deletes the specified environment and all its target roots and API keys.
    The last environment in a workspace cannot be deleted.

    Examples:
        stardag environment delete staging
        stardag environment delete staging --force
    """
    workspace_id = _get_workspace_id()
    client, api_url, _ = get_authenticated_client()

    try:
        # First, list environments to resolve slug to ID
        response = client.get(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments"
        )

        if response.status_code != 200:
            _handle_api_error(response, "environments")
            raise typer.Exit(1)

        environments = response.json()
        target_env = None
        for env in environments:
            if env["slug"] == environment or env["id"] == environment:
                target_env = env
                break

        if not target_env:
            typer.echo(f"Error: Environment '{environment}' not found.", err=True)
            typer.echo("")
            typer.echo("Available environments:")
            for env in environments:
                typer.echo(f"  {env['slug']} ({env['name']})")
            raise typer.Exit(1)

        if not force:
            typer.confirm(
                f"Delete environment '{target_env['name']}' ({target_env['slug']})? "
                "This will also delete all target roots and API keys.",
                abort=True,
            )

        response = client.delete(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{target_env['id']}"
        )

        if response.status_code == 204:
            typer.echo(
                f"Environment '{target_env['name']}' ({target_env['slug']}) deleted."
            )
        elif response.status_code == 400:
            detail = response.json().get("detail", "Bad request")
            typer.echo(f"Error: {detail}", err=True)
            raise typer.Exit(1)
        else:
            _handle_api_error(response, "environment")
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except typer.Abort:
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


# --- Target roots subcommands ---

target_roots_app = typer.Typer(help="Manage target roots for an environment")
app.add_typer(target_roots_app, name="target-roots")


def _list_target_roots_api(
    client: httpx.Client,
    api_url: str,
    workspace_id: str,
    environment_id: str,
) -> list[dict]:
    """Fetch target roots from the API."""
    response = client.get(
        f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots"
    )
    if response.status_code != 200:
        _handle_api_error(response, "target roots")
        raise typer.Exit(1)
    return response.json()


@target_roots_app.command("list")
def target_roots_list(
    env: str = typer.Option(
        None,
        "--env",
        "-e",
        help="Environment slug or ID (default: active environment)",
    ),
) -> None:
    """List target roots for an environment.

    Fetches the current target roots from the server.

    Examples:
        stardag environment target-roots list
        stardag environment target-roots list --env staging
    """
    workspace_id = _get_workspace_id()
    environment_id = _get_environment_id(env)
    client, api_url, _ = get_authenticated_client()

    try:
        roots = _list_target_roots_api(client, api_url, workspace_id, environment_id)
    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()

    if not roots:
        typer.echo("No target roots configured for this environment.")
        typer.echo("")
        typer.echo("Add one with: stardag environment target-roots add <name> <uri>")
        return

    typer.echo("Target Roots:")
    for root in roots:
        typer.echo(f"  {root['name']}: {root['uri_prefix']}")


@target_roots_app.command("add")
def target_roots_add(
    name: str = typer.Argument(..., help="Target root name (e.g., 'default', 's3')"),
    uri: str = typer.Argument(
        ..., help="URI prefix (e.g., 's3://bucket/path', '/local/path')"
    ),
    env: str = typer.Option(
        None,
        "--env",
        "-e",
        help="Environment slug or ID (default: active environment)",
    ),
) -> None:
    """Add a new target root to an environment.

    Examples:
        stardag environment target-roots add default s3://my-bucket/outputs
        stardag environment target-roots add s3 s3://my-bucket/data --env staging
    """
    workspace_id = _get_workspace_id()
    environment_id = _get_environment_id(env)
    client, api_url, _ = get_authenticated_client()

    try:
        response = client.post(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots",
            json={"name": name, "uri_prefix": uri},
        )

        if response.status_code == 201:
            typer.echo(f"Target root '{name}' added: {uri}")
            _sync_target_roots_cache(client, api_url, workspace_id, environment_id)
        else:
            _handle_api_error(response, f"target root '{name}'")
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


@target_roots_app.command("remove")
def target_roots_remove(
    name: str = typer.Argument(..., help="Target root name to remove"),
    env: str = typer.Option(
        None,
        "--env",
        "-e",
        help="Environment slug or ID (default: active environment)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Remove a target root by name.

    Examples:
        stardag environment target-roots remove s3
        stardag environment target-roots remove default --env staging --force
    """
    workspace_id = _get_workspace_id()
    environment_id = _get_environment_id(env)
    client, api_url, _ = get_authenticated_client()

    try:
        # List target roots to find the one by name
        roots = _list_target_roots_api(client, api_url, workspace_id, environment_id)

        target_root = None
        for root in roots:
            if root["name"] == name:
                target_root = root
                break

        if not target_root:
            typer.echo(f"Error: Target root '{name}' not found.", err=True)
            if roots:
                typer.echo("")
                typer.echo("Available target roots:")
                for root in roots:
                    typer.echo(f"  {root['name']}: {root['uri_prefix']}")
            raise typer.Exit(1)

        if not force:
            typer.confirm(
                f"Remove target root '{name}' ({target_root['uri_prefix']})?",
                abort=True,
            )

        response = client.delete(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots/{target_root['id']}"
        )

        if response.status_code == 204:
            typer.echo(f"Target root '{name}' removed.")
            _sync_target_roots_cache(client, api_url, workspace_id, environment_id)
        else:
            _handle_api_error(response, "target root")
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except typer.Abort:
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


@target_roots_app.command("set")
def target_roots_set(
    target_roots: list[str] = typer.Argument(
        None,
        help="Target roots as name=uri pairs (e.g., default=s3://bucket s3=s3://other)",
    ),
    json: str = typer.Option(
        None,
        "--json",
        "-j",
        help='JSON object mapping names to URIs (e.g., \'{"default": "s3://bucket"}\')',
    ),
    env: str = typer.Option(
        None,
        "--env",
        "-e",
        help="Environment slug or ID (default: active environment)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Set all target roots for an environment, replacing any existing ones.

    Provide target roots either as positional name=uri pairs or as a JSON object
    via --json. Existing target roots not in the new set will be removed.

    Examples:
        stardag environment target-roots set default=s3://bucket/outputs
        stardag environment target-roots set default=s3://bucket s3=s3://other
        stardag environment target-roots set --json '{"default": "s3://bucket/outputs"}'
        stardag environment target-roots set --env staging default=s3://staging/outputs
    """
    # Parse input
    if json and target_roots:
        typer.echo(
            "Error: Provide target roots either as arguments or --json, not both.",
            err=True,
        )
        raise typer.Exit(1)

    if json:
        try:
            desired = json_module.loads(json)
            if not isinstance(desired, dict):
                typer.echo(
                    "Error: --json must be a JSON object mapping names to URIs.",
                    err=True,
                )
                raise typer.Exit(1)
            for k, v in desired.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    typer.echo(
                        "Error: All keys and values in --json must be strings.",
                        err=True,
                    )
                    raise typer.Exit(1)
        except json_module.JSONDecodeError as e:
            typer.echo(f"Error: Invalid JSON: {e}", err=True)
            raise typer.Exit(1)
    elif target_roots:
        desired = _parse_target_roots(target_roots)
    else:
        typer.echo(
            "Error: Provide target roots as name=uri arguments or via --json.",
            err=True,
        )
        raise typer.Exit(1)

    workspace_id = _get_workspace_id()
    environment_id = _get_environment_id(env)
    client, api_url, _ = get_authenticated_client()

    try:
        # Get current target roots
        current_roots = _list_target_roots_api(
            client, api_url, workspace_id, environment_id
        )
        current_by_name = {root["name"]: root for root in current_roots}

        # Compute diff
        to_create = {n: u for n, u in desired.items() if n not in current_by_name}
        to_update = {
            n: u
            for n, u in desired.items()
            if n in current_by_name and current_by_name[n]["uri_prefix"] != u
        }
        to_delete = {n: current_by_name[n] for n in current_by_name if n not in desired}
        unchanged = {
            n: u
            for n, u in desired.items()
            if n in current_by_name and current_by_name[n]["uri_prefix"] == u
        }

        # Show summary and confirm
        if not to_create and not to_update and not to_delete:
            typer.echo("Target roots are already up to date.")
            return

        typer.echo("Changes to apply:")
        for name, uri in to_create.items():
            typer.echo(f"  + {name}: {uri}")
        for name, uri in to_update.items():
            old_uri = current_by_name[name]["uri_prefix"]
            typer.echo(f"  ~ {name}: {old_uri} -> {uri}")
        for name, root in to_delete.items():
            typer.echo(f"  - {name}: {root['uri_prefix']}")
        for name, uri in unchanged.items():
            typer.echo(f"    {name}: {uri} (unchanged)")

        if not force:
            typer.confirm("Apply these changes?", abort=True)

        # Apply changes
        errors = 0

        for name, root in to_delete.items():
            resp = client.delete(
                f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots/{root['id']}"
            )
            if resp.status_code == 204:
                typer.echo(f"  Removed '{name}'")
            else:
                _handle_api_error(resp, f"target root '{name}'")
                errors += 1

        for name, uri in to_update.items():
            root = current_by_name[name]
            resp = client.patch(
                f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots/{root['id']}",
                json={"uri_prefix": uri},
            )
            if resp.status_code == 200:
                typer.echo(f"  Updated '{name}'")
            else:
                _handle_api_error(resp, f"target root '{name}'")
                errors += 1

        for name, uri in to_create.items():
            resp = client.post(
                f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments/{environment_id}/target-roots",
                json={"name": name, "uri_prefix": uri},
            )
            if resp.status_code == 201:
                typer.echo(f"  Created '{name}'")
            else:
                _handle_api_error(resp, f"target root '{name}'")
                errors += 1

        if errors > 0:
            typer.echo(
                f"Completed with {errors} error(s). Run 'stardag environment target-roots list' to verify.",
                err=True,
            )
            raise typer.Exit(1)

        typer.echo("Target roots updated successfully.")

        # Update local cache
        _sync_target_roots_cache(client, api_url, workspace_id, environment_id, desired)

    except typer.Exit:
        raise
    except typer.Abort:
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


def _sync_target_roots_cache(
    client: httpx.Client,
    api_url: str,
    workspace_id: str,
    environment_id: str,
    target_roots: dict[str, str] | None = None,
) -> None:
    """Update the local target roots cache after a server-side change.

    If target_roots is None, re-fetches from the API to get the current state.
    """
    try:
        if target_roots is None:
            roots = _list_target_roots_api(
                client, api_url, workspace_id, environment_id
            )
            target_roots = {root["name"]: root["uri_prefix"] for root in roots}
        set_cached_target_roots(
            target_roots,
            registry_url=api_url,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
    except Exception:
        pass  # Cache update is best-effort
