"""Stardag CLI - Command line interface for Stardag.

Usage:
    stardag auth login [-r registry] [--api-url url]
    stardag auth logout [-r registry]
    stardag auth status [-r registry]
    stardag auth refresh [-r registry] [-w workspace]

    stardag config show
    stardag config registry add <name> --url <url>
    stardag config registry list
    stardag config registry remove <name>
    stardag config profile add <name> -r <registry> -w <workspace> -e <environment>
    stardag config profile list
    stardag config profile use <name>
    stardag config profile remove <name>
    stardag config list workspaces
    stardag config list environments

    stardag environment list
    stardag environment create <name> [--slug <slug>] [--target-root name=uri ...]
    stardag environment delete <slug-or-id> [--force]
    stardag environment target-roots list [--env <env>]
    stardag environment target-roots add <name> <uri> [--env <env>]
    stardag environment target-roots remove <name> [--env <env>]
    stardag environment target-roots set <name=uri ...> [--json <json>] [--env <env>]

    stardag concurrency-limits list [--holders] [-p profile] [-e env]
    stardag concurrency-limits set <key> <max_concurrent> [-p profile] [-e env]
    stardag concurrency-limits delete <key> [--yes] [-p profile] [-e env]
    stardag concurrency-limits holders <key> [--limit N] [-p profile] [-e env]
    stardag concurrency-limits evict <key> <task_id> [--yes] [-p profile] [-e env]

    stardag modal deploy <app_ref> [--name name] [-e env] [--stream-logs] [--tag tag] [-m]
    stardag modal stardag-api-key create [--modal-env env] [-w workspace] [-e env] [-p profile]

    stardag self-host up [--neon-api-key key] [--auth-mode local|oidc] [...]
    stardag self-host upgrade
    stardag self-host status
    stardag self-host destroy [--delete-secrets]

Configuration:
    Set STARDAG_PROFILE=<profile-name> to use a specific profile.
    Set STARDAG_API_URL, STARDAG_WORKSPACE_ID, STARDAG_ENVIRONMENT_ID
    for direct configuration (bypasses profiles).
    Set STARDAG_API_KEY for API key authentication.
"""

import typer

from stardag._cli import auth, config, environment, limits

# Main CLI app
app = typer.Typer(
    name="stardag",
    help="Stardag CLI - Declarative DAG framework for Python",
    no_args_is_help=True,
)

# Add subcommands
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(environment.app, name="environment")
app.add_typer(limits.app, name="concurrency-limits")

# Add modal subcommand only if modal is installed
try:
    from stardag._cli import modal

    app.add_typer(modal.app, name="modal")
except ImportError:
    pass

# Add self-host subcommand only if the selfhost extra is installed
try:
    import cryptography  # noqa: F401
    import modal as _modal  # noqa: F401

    from stardag._cli import selfhost

    app.add_typer(selfhost.app, name="self-host")
except ImportError:
    pass


@app.command()
def version() -> None:
    """Show the Stardag version."""
    try:
        from importlib.metadata import version as get_version

        ver = get_version("stardag")
    except Exception:
        ver = "unknown"

    typer.echo(f"stardag {ver}")


@app.callback()
def main() -> None:
    """Stardag CLI - Declarative DAG framework for Python.

    Use 'stardag auth login' to authenticate with the Stardag API.
    Use 'stardag config profile' commands to manage profiles.
    Set STARDAG_PROFILE environment variable to activate a profile.
    """
    pass


if __name__ == "__main__":
    app()
