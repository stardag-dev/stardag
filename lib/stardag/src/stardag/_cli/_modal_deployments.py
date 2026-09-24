"""The registry side of ``stardag modal deploy``: one deployment row per
deploy, created before it and activated after it (design.md, "The
deterministic scope"), and ``stardag modal deployments``."""

from typing import TYPE_CHECKING, Optional

import typer
from rich.console import Console

if TYPE_CHECKING:
    from stardag.integration.modal import StardagApp

console = Console()
error_console = Console(stderr=True)


def _deployment_registry(consequence: str = "deployment not recorded"):
    """The configured registry, or None (with a notice) when there is none
    to record the deployment in -- or, for ``stardag modal deployments``,
    to list from."""
    from stardag.registry import is_noop_registry, registry_provider

    registry = registry_provider.get()
    if is_noop_registry(registry):
        console.print(f"[dim]No registry configured; {consequence}.[/dim]")
        return None
    return registry


def _create_deployment(
    registry, stardag_app_instance: "StardagApp", app_name: str
) -> None:
    """Record the deployment **before** the deploy (design.md, "The
    deterministic scope"): the registry assigns it its ``generation`` now,
    so a record that lands late cannot roll a build back to older code.
    A failure here stops the command before anything is deployed."""
    deployment_id = stardag_app_instance.deployment_id
    try:
        info = registry.deployment_create(
            kind="modal",
            app_name=app_name,
            code_id=stardag_app_instance.code_id,
            deployment_id=deployment_id,
        )
    except Exception as e:
        error_console.print(
            f"[bold red]Could not record deployment {deployment_id} of "
            f"{app_name} in the registry:[/bold red] {type(e).__name__}: {e}\n"
            "Nothing was deployed. Re-run once the registry is reachable."
        )
        raise typer.Exit(1)
    console.print(
        f"[cyan]Recorded deployment[/cyan] {info.id} of {info.app_name} "
        f"(generation {info.generation}, code {info.code_id[:12]})"
    )


def _activate_deployment(
    registry, deployment_id, app_name: str, *, modal_app_id: str | None = None
) -> None:
    """Mark the deployment live now that the deploy succeeded, recording the
    Modal app id only the finished deploy knows. A failure exits non-zero:
    until it lands, no tick of the new code can plan (they exit
    ``superseded``) and running reactive builds stay on the old code.
    Re-sending is idempotent."""
    try:
        info = registry.deployment_activate(deployment_id, modal_app_id=modal_app_id)
    except Exception as e:
        error_console.print(
            f"[bold red]Deployed {app_name} but could not activate deployment "
            f"{deployment_id} in the registry:[/bold red] {type(e).__name__}: "
            f"{e}\nUntil it is activated, the new code's scheduler ticks exit "
            "'superseded' and running reactive builds stay on the previous "
            "deployment. Re-run `stardag modal deploy` once the registry is "
            "reachable."
        )
        raise typer.Exit(1)
    console.print(
        f"[green]Activated deployment[/green] {info.id} "
        f"({'current' if info.is_current else 'not current'})"
    )


def deployments(
    app_name: Optional[str] = typer.Option(None, "--app", help="Only this app."),
    current: bool = typer.Option(
        False, "--current", help="Only each app's current deployment."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the API payload as JSON on stdout."
    ),
) -> None:
    """List the Modal deployments recorded in the registry, newest first.

    An alias of ``stardag deployments list --kind modal``. Reads
    ``GET /deployments``; writes nothing. An app's current deployment is
    its activated one with the highest generation, and running reactive
    builds roll over to it at their next scheduler tick. A row with no
    activation is a deploy whose record was created but never activated.
    """
    from stardag._cli.deployments import render_deployments

    registry = _deployment_registry("no deployments to list")
    if registry is None:
        return
    rows = registry.deployment_list(kind="modal", app_name=app_name, current=current)
    render_deployments(rows, json_output=json_output)
