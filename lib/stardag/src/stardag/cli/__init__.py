"""Stardag CLI - Command line interface for Stardag.

Usage:
    stardag auth login [-r registry] [--api-url url]
    stardag auth logout [-r registry]
    stardag auth status [-r registry]
    stardag auth refresh [-r registry] [-o org]

    stardag registry add <name> --url <url>
    stardag registry list
    stardag registry remove <name>

    stardag config show
    stardag config profile add <name> -r <registry> -o <org> -w <workspace>
    stardag config profile list
    stardag config profile use <name>
    stardag config profile remove <name>
    stardag config target-roots list
    stardag config target-roots sync
    stardag config list organizations
    stardag config list workspaces

Configuration:
    Set STARDAG_PROFILE=<profile-name> to use a specific profile.
    Set STARDAG_REGISTRY_URL, STARDAG_ORGANIZATION_ID, STARDAG_WORKSPACE_ID
    for direct configuration (bypasses profiles).
    Set STARDAG_API_KEY for API key authentication.
"""

try:
    import typer
except ImportError:
    raise ImportError(
        "Typer is required for the CLI. Install with: pip install stardag[cli]"
    )

from stardag.cli import auth, config, registry

# Main CLI app
app = typer.Typer(
    name="stardag",
    help="Stardag CLI - Declarative DAG framework for Python",
    no_args_is_help=True,
)

# Add subcommands
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(registry.app, name="registry")


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
