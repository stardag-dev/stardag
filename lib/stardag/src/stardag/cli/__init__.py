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

    stardag registry list
    stardag registry current
    stardag registry add <name> --api-url <url>
    stardag registry use <name>
    stardag registry delete <name>
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
    Use 'stardag config' commands to manage your active organization and workspace.
    """
    pass


if __name__ == "__main__":
    app()
