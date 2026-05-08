"""Stardag Modal CLI - Command line interface for Stardag Modal integration.

This module wraps Modal's deploy command to work with StardagApp instances.
Instead of deploying a modal.App directly, it finds a StardagApp instance
in the specified module and deploys its underlying modal_app.

The key difference from `modal deploy` is that this CLI:
1. Calls `finalize()` on the StardagApp before deployment
2. Injects profile-based environment variables as a Modal secret

Usage:
    stardag modal deploy my_script.py
    stardag modal deploy -m my_package.my_mod
    stardag modal deploy my_script.py::stardag_app
    stardag modal deploy my_script.py --profile production
    stardag modal stardag-api-key create [--modal-env env] [-w workspace] [-e env]
"""

import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console

from stardag._cli._helpers import get_authenticated_client
from stardag._cli.credentials import (
    list_registries,
    resolve_environment_slug_to_id,
    resolve_workspace_slug_to_id,
)
from stardag.config.cache import get_cached_slugs
from stardag.config.loader import clear_config_cache, get_config
from stardag.integration.modal import StardagApp

# Check if modal is available
try:
    import modal  # noqa: F401

    MODAL_AVAILABLE = True
except ImportError:
    MODAL_AVAILABLE = False

# CLI app for modal subcommand
app = typer.Typer(
    name="modal",
    help="Stardag Modal integration commands",
    no_args_is_help=True,
)

console = Console()
error_console = Console(stderr=True)

# Sub-typer for stardag-api-key management
stardag_api_key_app = typer.Typer(help="Manage Stardag API keys in Modal")
app.add_typer(stardag_api_key_app, name="stardag-api-key")


def _registry_name_for_url(api_url: str | None) -> str | None:
    """Look up the configured registry name for a given URL.

    The id-cache is keyed by registry *name* (the TOML key under ``[registry.<name>]``)
    rather than URL, so we reverse-lookup the name from the URL the resolved IDs were
    fetched against. Returns ``None`` if no registry with a matching URL is configured
    (e.g. STARDAG_API_URL is set without a corresponding TOML entry).
    """
    if not api_url:
        return None
    target = api_url.rstrip("/")
    for name, url in list_registries().items():
        if url.rstrip("/") == target:
            return name
    return None


def _resolve_display_slugs(
    api_url: str | None,
    workspace_id: str | None,
    environment_id: str | None,
) -> tuple[str | None, str | None]:
    """Reverse-lookup workspace/environment slugs for the resolved UUIDs.

    Why: the resolved UUIDs may come from env vars or a custom config_provider
    override and have no relation to the active profile's TOML slugs. Reading
    `prof["environment"]` could display a slug for a totally different env
    than the resolved UUID. We also can't trust ``get_config().context.registry_name``
    because callers like ``deploy(--profile foo)`` and ``stardag_api_key_create``
    resolve IDs under a temporarily-overridden profile, then restore the env
    before this function runs — leaving ``get_config()`` pointing at the
    *active* profile, which may use a different registry. Pin the registry to
    the URL the IDs were actually resolved against.
    """
    registry_name = _registry_name_for_url(api_url)
    if not registry_name:
        return None, None
    return get_cached_slugs(registry_name, workspace_id, environment_id)


def _print_stardag_context(env_vars: dict[str, str]) -> None:
    """Print stardag context info (registry, workspace, environment, target roots)."""
    import json

    registry = env_vars.get("STARDAG_API_URL", "none (local mode)")
    ws_id = env_vars.get("STARDAG_WORKSPACE_ID", "N/A")
    env_id = env_vars.get("STARDAG_ENVIRONMENT_ID", "N/A")

    ws_slug, env_slug = _resolve_display_slugs(
        env_vars.get("STARDAG_API_URL"), ws_id, env_id
    )

    console.print(f"[dim]  Registry: {registry}[/dim]")

    ws_display = f"{ws_id} ({ws_slug})" if ws_slug else ws_id
    console.print(f"[dim]  Workspace: {ws_display}[/dim]")

    env_display = f"{env_id} ({env_slug})" if env_slug else env_id
    console.print(f"[dim]  Environment: {env_display}[/dim]")

    if "STARDAG_TARGET_ROOTS" in env_vars:
        target_roots = json.loads(env_vars["STARDAG_TARGET_ROOTS"])
        console.print("[dim]  Target roots:[/dim]")
        for name, uri in target_roots.items():
            console.print(f"[dim]    {name}: {uri}[/dim]")


def _resolve_stardag_context(
    profile: str | None,
    workspace_override: str | None,
    env_override: str | None,
) -> tuple[httpx.Client, str, str, str]:
    """Resolve stardag context and get authenticated client.

    Returns (client, api_url, workspace_id, environment_id).
    """
    old_profile = os.environ.get("STARDAG_PROFILE")
    try:
        if profile:
            os.environ["STARDAG_PROFILE"] = profile
            clear_config_cache()

        client, api_url, access_token = get_authenticated_client()
        config = get_config()

        registry_name = config.context.registry_name
        reg = config.registry
        user = reg.auth.user_email if reg else None
        workspace_id = reg.workspace_id if reg else None
        environment_id = reg.environment_id if reg else None

        if workspace_override and registry_name:
            resolved = resolve_workspace_slug_to_id(
                registry_name, workspace_override, user
            )
            if not resolved:
                error_console.print(
                    f"[bold red]Could not resolve workspace: {workspace_override}[/bold red]"
                )
                raise typer.Exit(1)
            workspace_id = resolved

        if env_override and registry_name and workspace_id:
            resolved = resolve_environment_slug_to_id(
                registry_name, workspace_id, env_override, user, access_token
            )
            if not resolved:
                error_console.print(
                    f"[bold red]Could not resolve environment: {env_override}[/bold red]"
                )
                raise typer.Exit(1)
            environment_id = resolved

        if not workspace_id:
            error_console.print(
                "[bold red]No workspace configured.[/bold red]\n"
                "Set STARDAG_PROFILE or use -w/--stardag-workspace."
            )
            raise typer.Exit(1)

        if not environment_id:
            error_console.print(
                "[bold red]No environment configured.[/bold red]\n"
                "Set STARDAG_PROFILE or use -e/--stardag-env."
            )
            raise typer.Exit(1)

        return client, api_url, workspace_id, environment_id
    finally:
        if old_profile is not None:
            os.environ["STARDAG_PROFILE"] = old_profile
        else:
            os.environ.pop("STARDAG_PROFILE", None)
        if profile:
            clear_config_cache()


def _create_stardag_api_key(
    client: httpx.Client,
    api_url: str,
    workspace_id: str,
    environment_id: str,
    key_name: str,
) -> tuple[str, str]:
    """Create a Stardag API key. Returns (full_key, key_prefix)."""
    url = (
        f"{api_url}/api/v1/ui/workspaces/{workspace_id}"
        f"/environments/{environment_id}/api-keys"
    )
    response = client.post(url, json={"name": key_name})

    if response.status_code == 201:
        data = response.json()
        return data["key"], data["key_prefix"]

    if response.status_code == 401:
        error_console.print(
            "[bold red]Authentication error.[/bold red]\n"
            "Try running: stardag auth refresh"
        )
        raise typer.Exit(1)

    if response.status_code == 403:
        error_console.print(
            "[bold red]Permission denied.[/bold red]\n"
            "You need admin or higher role to create API keys."
        )
        raise typer.Exit(1)

    error_console.print(
        f"[bold red]API error ({response.status_code}):[/bold red] {response.text}"
    )
    raise typer.Exit(1)


def _push_modal_secret(
    secret_name: str,
    env_dict: dict[str, str],
    modal_env: str | None,
) -> None:
    """Push a named secret to Modal (idempotent: delete + create)."""
    import modal

    environment_name = modal_env or ""
    try:
        modal.Secret.objects.delete(
            secret_name, allow_missing=True, environment_name=environment_name
        )
        modal.Secret.objects.create(
            secret_name, env_dict, environment_name=environment_name
        )
    except Exception as e:
        error_console.print(f"[bold red]Modal error: {e}[/bold red]")
        raise


@stardag_api_key_app.command("create")
def stardag_api_key_create(
    stardag_workspace: Optional[str] = typer.Option(
        None,
        "-w",
        "--stardag-workspace",
        help="Stardag workspace slug or ID. Defaults to active profile.",
    ),
    stardag_env: Optional[str] = typer.Option(
        None,
        "-e",
        "--stardag-env",
        help="Stardag environment slug or ID. Defaults to active profile.",
    ),
    stardag_profile: Optional[str] = typer.Option(
        None,
        "-p",
        "--stardag-profile",
        help="Stardag profile to use. Defaults to active profile.",
    ),
    modal_env: Optional[str] = typer.Option(
        None,
        "--modal-env",
        help="Modal environment name. Defaults to Modal's default environment.",
    ),
    secret_name: str = typer.Option(
        "stardag-api-key",
        "--secret-name",
        help="Name of the Modal secret to create.",
    ),
    api_key_name: Optional[str] = typer.Option(
        None,
        "--api-key-name",
        help='Name for the Stardag API key. Defaults to "modal-{modal_env}".',
    ),
) -> None:
    """Create a Stardag API key and push it as a Modal secret."""
    if not MODAL_AVAILABLE:
        error_console.print(
            "[bold red]Modal is not installed.[/bold red]\n"
            "Install it with: pip install stardag[modal]"
        )
        raise typer.Exit(1)

    client, api_url, ws_id, env_id = _resolve_stardag_context(
        stardag_profile, stardag_workspace, stardag_env
    )

    try:
        if api_key_name is None:
            api_key_name = f"modal-{modal_env or 'default'}"

        ws_slug, env_slug = _resolve_display_slugs(api_url, ws_id, env_id)
        ws_display = f"{ws_id} ({ws_slug})" if ws_slug else ws_id
        env_display = f"{env_id} ({env_slug})" if env_slug else env_id

        console.print("\n[cyan]Using stardag context:[/cyan]")
        console.print(f"  Registry: {api_url}")
        console.print(f"  Workspace: {ws_display}")
        console.print(f"  Environment: {env_display}")
        console.print()

        full_key, key_prefix = _create_stardag_api_key(
            client, api_url, ws_id, env_id, api_key_name
        )
        console.print(
            f'[green]Created Stardag API key: "{api_key_name}" '
            f"(prefix: {key_prefix})[/green]"
        )

        try:
            _push_modal_secret(secret_name, {"STARDAG_API_KEY": full_key}, modal_env)
        except Exception:
            error_console.print(
                "[yellow]API key was created successfully. Key value:[/yellow]"
            )
            error_console.print(f"[yellow]  {full_key}[/yellow]")
            error_console.print("[yellow]Create the Modal secret manually:[/yellow]")
            error_console.print(
                f"[yellow]  modal secret create {secret_name} "
                f'STARDAG_API_KEY="{full_key}"[/yellow]'
            )
            raise typer.Exit(1)

        modal_env_display = f" (Modal environment: {modal_env})" if modal_env else ""
        console.print(
            f'[green]Pushed Modal secret: "{secret_name}"{modal_env_display}[/green]'
        )
        console.print("\n[dim]Use in your app:[/dim]")
        console.print(f'[dim]  modal.Secret.from_name("{secret_name}")[/dim]')
    finally:
        client.close()


def _import_file_or_module(file_or_module: str, use_module_mode: bool) -> object:
    """Import a file or module and return the module object.

    Based on Modal's import_refs.import_file_or_module but simplified.
    """
    if "" not in sys.path:
        # Ensure current working directory is on sys.path
        sys.path.insert(0, "")

    if not file_or_module.endswith(".py") or use_module_mode:
        # Import as module
        module = importlib.import_module(file_or_module)
    else:
        # Import as script file
        full_path = Path(file_or_module).resolve()
        if "." in full_path.name.removesuffix(".py"):
            raise typer.BadParameter(
                f"Invalid source filename: {full_path.name!r}. "
                "Source filename cannot contain additional period characters."
            )
        sys.path.insert(0, str(full_path.parent))

        module_name = inspect.getmodulename(file_or_module)
        if module_name is None:
            raise typer.BadParameter(
                f"Cannot determine module name for: {file_or_module}"
            )

        spec = importlib.util.spec_from_file_location(module_name, file_or_module)
        if spec is None or spec.loader is None:
            raise typer.BadParameter(f"Cannot load module spec for: {file_or_module}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    return module


def _find_stardag_app(module: object, object_path: str) -> StardagApp:
    """Find a StardagApp instance in the module.

    Args:
        module: The imported module to search
        object_path: Optional specific variable name to look for

    Returns:
        The StardagApp instance

    Raises:
        typer.Exit: If no StardagApp is found or the specified name is invalid
    """
    if object_path:
        # Look for specific variable name
        if not hasattr(module, object_path):
            error_console.print(
                f"[bold red]Could not find '{object_path}' in module.[/bold red]"
            )
            raise typer.Exit(1)

        obj = getattr(module, object_path)
        if not isinstance(obj, StardagApp):
            error_console.print(
                f"[bold red]'{object_path}' is not a StardagApp instance "
                f"(got {type(obj).__name__}).[/bold red]"
            )
            raise typer.Exit(1)

        return obj

    # Search for StardagApp instances in module
    stardag_apps: list[tuple[str, StardagApp]] = []
    for name, obj in inspect.getmembers(module):
        if isinstance(obj, StardagApp):
            stardag_apps.append((name, obj))

    if len(stardag_apps) == 0:
        error_console.print(
            "[bold red]No StardagApp instance found in module.[/bold red]\n"
            "Make sure your module contains a StardagApp instance, e.g.:\n"
            "  stardag_app = StardagApp('my-app', ...)"
        )
        raise typer.Exit(1)

    if len(stardag_apps) == 1:
        return stardag_apps[0][1]

    # Multiple apps found - require explicit specification
    app_names = [name for name, _ in stardag_apps]
    error_console.print(
        f"[bold red]Multiple StardagApp instances found: {app_names}[/bold red]\n"
        "Please specify which one to deploy using ::<name> syntax, e.g.:\n"
        f"  stardag modal deploy your_module::{app_names[0]}"
    )
    raise typer.Exit(1)


def _parse_app_ref(app_ref: str) -> tuple[str, str]:
    """Parse app reference into file/module and object path.

    Args:
        app_ref: Reference like "my_script.py" or "my_script.py::stardag_app"

    Returns:
        Tuple of (file_or_module, object_path)
    """
    if "::" in app_ref:
        parts = app_ref.split("::", 1)
        return parts[0], parts[1]
    elif ":" in app_ref and not app_ref.startswith(":"):
        # Catch common mistake of using single colon
        raise typer.BadParameter(
            f"Invalid reference: {app_ref}. Did you mean '::' instead of ':'?"
        )
    return app_ref, ""


@app.command("deploy")
def deploy(
    app_ref: str = typer.Argument(
        ...,
        help="Path to a Python file with a StardagApp to deploy",
    ),
    name: Optional[str] = typer.Option(
        None,
        help="Name of the deployment. Defaults to the app name.",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "-p",
        "--profile",
        help=(
            "Stardag profile to use for environment variables. "
            "The profile's registry URL, workspace ID, and environment ID "
            "will be injected as a Modal secret."
        ),
    ),
    env: Optional[str] = typer.Option(
        None,
        "-e",
        "--env",
        help=(
            "Modal environment to interact with. If not specified, Modal will use "
            "the default environment of your current profile, or the "
            "MODAL_ENVIRONMENT variable."
        ),
    ),
    stream_logs: bool = typer.Option(
        False,
        "--stream-logs/--no-stream-logs",
        help="Stream logs from the app upon deployment.",
    ),
    tag: str = typer.Option(
        "",
        help="Tag the deployment with a version.",
    ),
    use_module_mode: bool = typer.Option(
        False,
        "-m",
        help="Interpret argument as a Python module path instead of a file/script path",
    ),
) -> None:
    """Deploy a Stardag Modal application.

    This command finds a StardagApp instance in the specified module, finalizes it
    with profile-based environment variables, and deploys the underlying Modal app.

    The --profile option injects environment variables from the specified stardag
    profile (registry URL, workspace ID, environment ID) as a Modal secret. This
    allows the same app definition to be deployed to different stardag environments.

    Usage:
        stardag modal deploy my_script.py
        stardag modal deploy -m my_package.my_mod
        stardag modal deploy my_script.py::stardag_app
        stardag modal deploy my_script.py --profile production
    """
    if not MODAL_AVAILABLE:
        error_console.print(
            "[bold red]Modal is not installed.[/bold red]\n"
            "Install it with: pip install stardag[modal]"
        )
        raise typer.Exit(1)

    # Import modal dependencies now that we know modal is available
    import modal
    from modal.cli.utils import stream_app_logs
    from modal.environments import ensure_env
    from modal.output import enable_output
    from modal.runner import deploy_app

    from stardag.integration.modal import get_profile_env_vars

    # Parse the app reference
    file_or_module, object_path = _parse_app_ref(app_ref)

    # Ensure environment is set (this affects lookups)
    env = ensure_env(env)

    # Import the module
    try:
        module = _import_file_or_module(file_or_module, use_module_mode)
    except Exception as e:
        error_console.print(f"[bold red]Error importing module:[/bold red] {e}")
        raise typer.Exit(1)

    # Find the StardagApp
    stardag_app_instance = _find_stardag_app(module, object_path)

    # Check if already finalized (e.g., legacy usage)
    if stardag_app_instance.is_finalized:
        console.print(
            "[yellow]Warning: StardagApp was already finalized. "
            "Profile secrets will not be injected.[/yellow]"
        )
    else:
        # Create profile secret if profile specified
        extra_secrets: list[modal.Secret] = []
        if profile:
            console.print(f"[cyan]Using stardag profile: {profile}[/cyan]")
            env_vars = get_profile_env_vars(profile)
            if env_vars:
                _print_stardag_context(env_vars)
                extra_secrets.append(
                    modal.Secret.from_dict(dict(env_vars))  # type: ignore[arg-type]
                )
            else:
                error_console.print(
                    f"[bold red]Profile '{profile}' not found or has no configuration.[/bold red]"
                )
                raise typer.Exit(1)
        else:
            # Use active profile (from STARDAG_PROFILE env var or default)
            env_vars = get_profile_env_vars()
            if env_vars:
                console.print("[cyan]Using active stardag profile[/cyan]")
                _print_stardag_context(env_vars)
                extra_secrets.append(
                    modal.Secret.from_dict(dict(env_vars))  # type: ignore[arg-type]
                )

        # Finalize the app with profile secrets
        finalize_result = stardag_app_instance.finalize(extra_secrets=extra_secrets)

        if finalize_result.volumes:
            console.print("[cyan]Modal volumes:[/cyan]")
            for root_key, vol in finalize_result.volumes.items():
                console.print(f"[dim]  {root_key}: {vol.name}[/dim]")

        if finalize_result.volume_mounts:
            console.print("[cyan]Modal volumes (auto-mounted):[/cyan]")
            for mount_path, vol_name in finalize_result.volume_mounts.items():
                console.print(f"[dim]  {vol_name} -> {mount_path}[/dim]")

        console.print("[cyan]Functions:[/cyan]")
        for func_name in finalize_result.functions:
            console.print(f"[dim]  {func_name}[/dim]")

    # Get the underlying Modal app
    modal_app = stardag_app_instance.modal_app

    # Determine deployment name
    deployment_name = name or modal_app.name or ""
    if not deployment_name:
        error_console.print(
            "[bold red]Deployment name required.[/bold red]\n"
            "Either supply --name on the command line or set a name on the StardagApp:\n"
            '  stardag_app = StardagApp("my-app-name", ...)'
        )
        raise typer.Exit(1)

    # Deploy the app
    with enable_output():
        res = deploy_app(
            modal_app, name=deployment_name, environment_name=env or "", tag=tag
        )

    console.print(f"[green]Deployed {deployment_name}[/green]")

    if stream_logs:
        # stream_app_logs is wrapped with @synchronizer.create_blocking, making it sync
        stream_app_logs(app_id=res.app_id, app_logs_url=res.app_logs_url)  # type: ignore[unused-coroutine]
