"""Stardag CLI - Command line interface for Stardag.

Usage:
    stardag auth login --api-key sk_xxx
    stardag auth status
    stardag auth logout
"""

try:
    import typer
except ImportError:
    raise ImportError(
        "Typer is required for the CLI. Install with: pip install stardag[cli]"
    )

from stardag.cli import auth

# Main CLI app
app = typer.Typer(
    name="stardag",
    help="Stardag CLI - Declarative DAG framework for Python",
    no_args_is_help=True,
)

# Add subcommands
app.add_typer(auth.app, name="auth")


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
    """
    pass


if __name__ == "__main__":
    app()
