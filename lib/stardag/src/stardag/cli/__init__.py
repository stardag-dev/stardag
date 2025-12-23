"""Stardag CLI - Command line interface for Stardag.

Usage:
    stardag auth login
    stardag auth status
    stardag auth logout

    stardag config get
    stardag config sync
    stardag config set organization <org-id-or-slug>
    stardag config set workspace <workspace-id-or-slug>
    stardag config list organizations
    stardag config list workspaces
    stardag config list target-roots

    stardag profile list
    stardag profile current
    stardag profile add <name> --api-url <url>
    stardag profile use <name>
    stardag profile delete <name>
"""

try:
    import typer
except ImportError:
    raise ImportError(
        "Typer is required for the CLI. Install with: pip install stardag[cli]"
    )

from stardag.cli import auth, config, profile

# Main CLI app
app = typer.Typer(
    name="stardag",
    help="Stardag CLI - Declarative DAG framework for Python",
    no_args_is_help=True,
)

# Add subcommands
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(profile.app, name="profile")


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
    Use 'stardag config' commands to manage your active organization and workspace.
    """
    pass


if __name__ == "__main__":
    app()
