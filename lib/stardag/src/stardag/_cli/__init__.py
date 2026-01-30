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
    stardag config target-roots list
    stardag config target-roots sync
    stardag config list workspaces
    stardag config list environments

Configuration:
    Set STARDAG_PROFILE=<profile-name> to use a specific profile.
    Set STARDAG_REGISTRY_URL, STARDAG_WORKSPACE_ID, STARDAG_ENVIRONMENT_ID
    for direct configuration (bypasses profiles).
    Set STARDAG_API_KEY for API key authentication.
"""

import typer

from stardag._cli import auth, config

# Main CLI app
app = typer.Typer(
    name="stardag",
    help="Stardag CLI - Declarative DAG framework for Python",
    no_args_is_help=True,
)

# Add subcommands
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")


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
