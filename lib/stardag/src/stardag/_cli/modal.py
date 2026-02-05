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
"""

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

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
                console.print(
                    f"[dim]  Registry URL: {env_vars.get('STARDAG_REGISTRY_URL', 'N/A')}[/dim]"
                )
                console.print(
                    f"[dim]  Workspace ID: {env_vars.get('STARDAG_WORKSPACE_ID', 'N/A')}[/dim]"
                )
                console.print(
                    f"[dim]  Environment ID: {env_vars.get('STARDAG_ENVIRONMENT_ID', 'N/A')}[/dim]"
                )
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
                console.print(
                    f"[dim]  Registry URL: {env_vars.get('STARDAG_REGISTRY_URL', 'N/A')}[/dim]"
                )
                console.print(
                    f"[dim]  Workspace ID: {env_vars.get('STARDAG_WORKSPACE_ID', 'N/A')}[/dim]"
                )
                console.print(
                    f"[dim]  Environment ID: {env_vars.get('STARDAG_ENVIRONMENT_ID', 'N/A')}[/dim]"
                )
                extra_secrets.append(
                    modal.Secret.from_dict(dict(env_vars))  # type: ignore[arg-type]
                )

        # Finalize the app with profile secrets
        stardag_app_instance.finalize(extra_secrets=extra_secrets)

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
